"""Explicit budget rules scoped to a spreadsheet/tab, never to reused channels."""
import json


def draft_key(entry):
    if not entry or not entry.get('sheet_id') or not entry.get('tab'):
        return None
    return json.dumps([entry['sheet_id'], entry['tab']], separators=(',', ':'))


def budget_bump(rules, entry):
    rule = rules.get(draft_key(entry), {})
    return rule.get('bump', 0) if rule.get('enabled') is True else 0


def configure(rules, entry, enabled):
    key = draft_key(entry)
    if key is None:
        raise ValueError('Link this channel to a sheet first with !setsheet.')
    previous = rules.get(key, {})
    rules[key] = {'enabled': enabled,
                  'bump': previous.get('bump', 0) if enabled and previous.get('enabled') else 0,
                  'last_flux_message_id': previous.get('last_flux_message_id', 0)}


def apply_flux(rules, entry, message_id, amount):
    key = draft_key(entry)
    rule = rules.get(key, {})
    if rule.get('enabled') is not True or message_id <= rule.get('last_flux_message_id', 0):
        return None
    rule['bump'] = rule.get('bump', 0) + amount
    rule['last_flux_message_id'] = message_id
    return rule['bump']
