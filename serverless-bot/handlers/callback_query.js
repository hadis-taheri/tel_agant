// handlers/callback_query.js — runs when the user taps an inline button.
// callback_query.message is the message the keyboard is attached to, so we
// edit it in place (editMessageText) rather than sending a new message each
// tap -- keeps the chat from filling up with menu spam.

import { api, BotApiError } from 'sdk';
import { getSubscriber, ensureSubscriber, setAlarmHour, setActive } from 'lib/supabase';
import {
  mainMenuText,
  mainMenuKeyboard,
  hourPickerKeyboard,
  statusText,
  backKeyboard,
} from 'lib/menu';

async function edit(chatId, messageId, text, replyMarkup) {
  try {
    await api.editMessageText({
      chat_id: chatId,
      message_id: messageId,
      text,
      reply_markup: replyMarkup,
    });
  } catch (e) {
    // 400 here is almost always "message is not modified" (double-tap on the
    // same button) -- harmless. Anything else is a real failure.
    if (!(e instanceof BotApiError) || e.code !== 400) throw e;
  }
}

export default async function (callbackQuery, ctx) {
  const chatId = callbackQuery.message.chat.id;
  const messageId = callbackQuery.message.message_id;
  const data = callbackQuery.data ?? '';

  // Always ack the tap first so Telegram stops showing the loading spinner,
  // regardless of what happens below.
  await api.answerCallbackQuery({ callback_query_id: callbackQuery.id });

  if (data === 'set_hour') {
    await edit(chatId, messageId, 'چه ساعتی (به وقت ایران) خلاصه‌ی روزانه برات ارسال بشه؟', hourPickerKeyboard());
    return;
  }

  if (data.startsWith('hour:')) {
    const hour = Number(data.slice('hour:'.length));
    await setAlarmHour(chatId, hour);
    const subscriber = await getSubscriber(chatId);
    await edit(
      chatId,
      messageId,
      `تنظیم شد ✅ هر روز ساعت ${String(hour).padStart(2, '0')}:۰۰ به وقت ایران، خلاصه‌ی ۲۴ ساعت اخیر برات میاد.\n\n${mainMenuText(subscriber)}`,
      mainMenuKeyboard(),
    );
    return;
  }

  if (data === 'status') {
    const subscriber = await ensureSubscriber(chatId);
    await edit(chatId, messageId, statusText(subscriber), backKeyboard());
    return;
  }

  if (data === 'unsubscribe') {
    await setActive(chatId, false);
    await edit(
      chatId,
      messageId,
      'اشتراکت لغو شد. هر وقت خواستی برگرد و یه ساعت آلارم جدید تنظیم کن.',
      backKeyboard(),
    );
    return;
  }

  if (data === 'main') {
    const subscriber = await ensureSubscriber(chatId);
    await edit(chatId, messageId, mainMenuText(subscriber), mainMenuKeyboard());
    return;
  }
}
