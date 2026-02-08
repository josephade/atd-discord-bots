require('dotenv').config();
const Discord = require('discord.js');
const { generateShotmap, handleShotmapCommand } = require('./utils/screenshot');
const fs = require('fs-extra');
const path = require('path');

// ==============================
// 🔍 ENV SANITY CHECKS (RUN FIRST)
// ==============================

console.log('================ ENV SANITY CHECKS ================');

if (!process.env.DISCORD_TOKEN) {
  console.error('❌ DISCORD_TOKEN is MISSING');
  process.exit(1);
}

const token = process.env.DISCORD_TOKEN;

console.log('✅ DISCORD_TOKEN exists');
console.log('🔢 Token length:', token.length);
console.log('🔍 Starts with:', token.slice(0, 5));
console.log('🔍 Ends with:', token.slice(-5));

if (token.includes(' ')) {
  console.error('❌ DISCORD_TOKEN contains spaces');
  process.exit(1);
}

if (token.includes('\n')) {
  console.error('❌ DISCORD_TOKEN contains newline characters');
  process.exit(1);
}

if (token.startsWith('"') || token.endsWith('"')) {
  console.error('❌ DISCORD_TOKEN includes quotes');
  process.exit(1);
}

if (!token.startsWith('M')) {
  console.error('❌ DISCORD_TOKEN does NOT look like a bot token (should start with "M")');
  process.exit(1);
}

console.log('✅ Token sanity checks PASSED');
console.log('===================================================');

// Ensure cache directory exists
const cacheDir = path.join(__dirname, 'cache');
fs.ensureDirSync(cacheDir);

const client = new Discord.Client({
  intents: [
    Discord.GatewayIntentBits.Guilds,
    Discord.GatewayIntentBits.GuildMessages,
    Discord.GatewayIntentBits.MessageContent
  ],
  partials: [Discord.Partials.Channel]
});

client.once('ready', () => {
  console.log(`✅ Bot is online! Logged in as ${client.user.tag}`);
  console.log(`📊 Serving ${client.guilds.cache.size} servers`);
  client.user.setActivity('!shotmap help', { type: Discord.ActivityType.Watching });
});

client.on('messageCreate', async (message) => {
  // Ignore bot messages and DMs
  if (message.author.bot || !message.guild) return;
  
  const content = message.content.trim();
  
  // Help command
  if (content === '!shotmap help' || content === '!shotchart help') {
    const helpEmbed = new Discord.EmbedBuilder()
      .setColor('#FF6B00')
      .setTitle('🏀 NBA Visuals Shotmap Bot')
      .setDescription('Generate basketball shotmaps from nbavisuals.com')
      .addFields(
        { name: 'Usage', value: '`!shotmap <player> <year> [--modern]`' },
        { name: 'Examples', value: '`!shotmap lebron 2024`\n`!shotmap steph curry 2023 --modern`\n`!shotmap kobe 2010`\n`!shotmap devin booker 2022 --modern`' },
        { name: 'Flags', value: '• `--modern`: Toggle Modern Shotmap view (creates a different visualization)' },
        { name: 'Player Names', value: '• Use full names: "lebron james", "stephen curry"\n• Nicknames work too: "kobe", "steph", "kd"\n• Year must be between 2000-2025' },
        { name: 'Features', value: '• **Caching**: Fast repeat requests\n• **Modern View**: Alternate visualization with `--modern` flag\n• **All Players**: Works for any NBA player\n• **Regular Season**: 2000-2025 seasons available' },
        { name: 'Note', value: '• First request: ~15 seconds\n• Cached requests: Instant\n• Data from NBA Visuals analytics' }
      )
      .setFooter({ text: 'nbavisuals.com/shotmap • Use !shotmap help for this menu' })
      .setTimestamp();
    
    return message.reply({ embeds: [helpEmbed] });
  }
  
  // Check if command starts with !shotmap or !shotchart
  if (!content.startsWith('!shotmap ') && !content.startsWith('!shotchart ')) return;
  
  // Use the command parser from screenshot.js
  try {
    const imageBuffer = await handleShotmapCommand(content);
    
    // Parse command to create descriptive filename
    const parts = content.split(' ');
    let playerNameParts = [];
    let year = null;
    let modern = false;
    
    for (let i = 1; i < parts.length; i++) {
      const part = parts[i];
      if (part === '--modern') {
        modern = true;
      } else if (/^\d{4}$/.test(part)) {
        year = part;
      } else if (part !== '!shotmap' && part !== '!shotchart') {
        playerNameParts.push(part);
      }
    }
    
    const playerName = playerNameParts.join(' ');
    const modernText = modern ? ' (Modern Shotmap)' : '';
    
    // Send the image
    await message.reply({
      content: `📊 **Shotmap for ${playerName} (${year})${modernText}**`,
      files: [{
        attachment: imageBuffer,
        name: `shotmap_${playerName.replace(/\s+/g, '_')}_${year}${modern ? '_modern' : ''}.png`
      }]
    });
    
  } catch (error) {
    console.error('Error handling shotmap command:', error);
    
    // More specific error messages
    if (error.message.includes('Invalid command format')) {
      return message.reply('❌ **Invalid command!** Use: `!shotmap <player> <year>`\n**Example:** `!shotmap lebron james 2024`\n**Need help?** Use `!shotmap help`');
    } else if (error.message.includes('Year not specified')) {
      return message.reply('❌ **Year not specified!** Use: `!shotmap <player> <year>`\n**Example:** `!shotmap steph curry 2023`\n**Valid years:** 2000-2025');
    } else if (error.message.includes('Player name not specified')) {
      return message.reply('❌ **Player name not specified!** Use: `!shotmap <player> <year>`\n**Example:** `!shotmap kobe bryant 2010`\n**Need ideas?** Try: lebron, curry, kobe, durant');
    } else if (error.message.includes('timeout')) {
      return message.reply('❌ **Timeout!** The website took too long to respond. Try again in a moment.');
    } else if (error.message.includes('navigation')) {
      return message.reply('❌ **Website error!** Failed to load nbavisuals.com. The site might be down.');
    } else if (error.message.includes('Could not click Generate button')) {
      return message.reply('❌ **Button error!** Could not generate the shotmap. Make sure the player and year combination exists.\n**Try:** `!shotmap lebron james 2023`');
    } else {
      return message.reply(`❌ **Error:** ${error.message}\n**Need help?** Use \`!shotmap help\``);
    }
  }
});

// Error handling
client.on('error', console.error);
process.on('unhandledRejection', error => {
  console.error('Unhandled promise rejection:', error);
});

// Login to Discord
client.login(process.env.DISCORD_TOKEN).catch(error => {
  console.error('Failed to login:', error);
  process.exit(1);
});