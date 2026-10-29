const { ActionRowBuilder, ButtonBuilder, ButtonStyle } = require('discord.js');
const PAGE_IDS = { front: 'nfl-card-statline', quarters: 'nfl-card-quarters', drives: 'nfl-card-drives' };
const PAGE_LABELS = { front: 'Statline', quarters: 'Quarter by Quarter', drives: 'Drive Summary' };
class PlayerCards {
  constructor({ renderer, payload, prepareFaces = false, ttl = 8 * 3600000, limit = 200, now = Date.now }) {
    Object.assign(this, { renderer, payload, prepareFaces, ttl, limit, now });
    this.entries = new Map();
  }
  buttons(face) {
    return [new ActionRowBuilder().addComponents(...Object.keys(PAGE_IDS).filter(page => page !== face)
      .map(page => new ButtonBuilder().setCustomId(PAGE_IDS[page]).setLabel(PAGE_LABELS[page]).setStyle(ButtonStyle.Secondary)))];
  }
  prune() {
    for (const [id, entry] of this.entries) if (entry.expires <= this.now()) this.entries.delete(id);
    while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value);
  }
  enqueue(entry, fn) {
    const task = (entry.queue || Promise.resolve()).then(fn);
    entry.queue = task.catch(() => {});
    return task;
  }
  async image(entry, face) {
    const images = entry.images, model = entry.model;
    if (!images[face]) images[face] = await this.renderer.render({ ...model, face });
    return images[face];
  }
  prepare(entry) {
    if (!this.prepareFaces || entry.preparing) return;
    const version = entry.version, images = entry.images, model = entry.model;
    entry.preparing = true;
    setImmediate(async () => {
      try {
        for (const face of Object.keys(PAGE_IDS)) {
          if (entry.version !== version) return;
          if (!images[face]) images[face] = await this.renderer.render({ ...model, face });
        }
      } catch (error) { console.error('Flip prefetch:', error.message); }
      finally { entry.preparing = false; }
    });
  }
  async draw(entry, shouldUpdate = () => true) {
    const image = await this.image(entry, entry.face);
    if (!shouldUpdate()) return null;
    const body = { ...this.payload(entry.model, image, entry.watching), attachments: [], components: this.buttons(entry.face) };
    await entry.message.edit(body);
    return body;
  }
  stopWatching(messageId) {
    const entry = this.entries.get(messageId);
    if (entry) entry.watching = false;
  }
  async show(message, model, watching = false, shouldUpdate = () => true) {
    this.prune();
    if (!model.eventId || model.state === 'season') {
      const body = { ...this.payload(model, await this.renderer.render(model), watching), attachments: [], components: [] };
      await message.edit(body);
      return body;
    }
    let entry = this.entries.get(message.id);
    if (!entry) {
      entry = { message, face: 'front', expires: this.now() + this.ttl };
      this.entries.set(message.id, entry);
      this.prune();
    }
    return this.enqueue(entry, async () => {
      if (!shouldUpdate()) return null;
      const version = JSON.stringify(model);
      if (entry.version !== version) { entry.version = version; entry.images = {}; }
      entry.model = model;
      entry.watching = watching;
      const result = await this.draw(entry, shouldUpdate);
      if (result) this.prepare(entry);
      return result;
    });
  }
  async select(interaction) {
    const target = Object.keys(PAGE_IDS).find(page => PAGE_IDS[page] === interaction.customId);
    if (!target) return;
    this.prune();
    const entry = this.entries.get(interaction.message.id);
    if (!entry?.model) {
      await interaction.reply({ content: 'This card has expired or the bot restarted. Run !nfl or !watch again to get a new flip card.', flags: 64 });
      return;
    }
    await interaction.deferUpdate();
    try {
      await this.enqueue(entry, async () => {
        const previous = entry.face;
        entry.face = target;
        try { await this.draw(entry); }
        catch (error) { entry.face = previous; throw error; }
      });
    } catch (error) {
      console.error('Card page:', error.message);
      await interaction.followUp({ content: 'Could not load this page just now. Please try the button again.', flags: 64 });
    }
  }
}
module.exports = { PlayerCards, PAGE_IDS };
