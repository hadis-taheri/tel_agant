// handlers/message.js — runs on each `message` update (text messages,
// commands, etc. sent to the bot's private chat).

import { api } from 'sdk';
import { ensureSubscriber, setActive } from 'lib/supabase';
import { mainMenuText, mainMenuKeyboard } from 'lib/menu';

export default async function (message, ctx) {
  const chatId = message.chat.id;
  const text = (message.text ?? '').trim();

  if (text === '/stop') {
    await setActive(chatId, false);
    await api.sendMessage({
      chat_id: chatId,
      text: 'اشتراکت لغو شد. هر وقت خواستی دوباره فعالش کنی، کافیه یه ساعت آلارم جدید تنظیم کنی یا /start رو بزنی.',
    });
    return;
  }

  // /start, or literally anything else the user sends -- just re-show the
  // menu. ensureSubscriber() is idempotent: it only creates the row on the
  // very first contact and never touches an existing one, so this is safe
  // to call on every message.
  const subscriber = await ensureSubscriber(chatId);
  await api.sendMessage({
    chat_id: chatId,
    text: mainMenuText(subscriber),
    reply_markup: mainMenuKeyboard(),
  });
}
