// Shared menu text/keyboard builders, used by both handlers/message.js
// (/start) and handlers/callback_query.js (button navigation) so the two
// stay visually consistent.

const HOURS = Array.from({ length: 18 }, (_, i) => i + 6); // 06..23 -- picking
// a specific minute is meaningless here: the Python sender (digest.py) only
// wakes up ~4x/hour (see ../../.github/workflows/daily-digest.yml), so a
// finer-grained picker would promise precision the system can't deliver.

export function mainMenuText(subscriber) {
  const lines = [
    'سلام! این ربات هر روز خلاصه‌ی ۲۴ ساعت اخیرِ کانال رو سر ساعتی که انتخاب کنی برات می‌فرسته.',
    '',
  ];
  if (subscriber.alarm_hour == null) {
    lines.push('هنوز ساعت آلارمی تنظیم نکردی.');
  } else {
    lines.push(
      `⏰ ساعت آلارم فعلی: ${String(subscriber.alarm_hour).padStart(2, '0')}:۰۰ (به وقت ایران)`,
      subscriber.active ? '✅ اشتراک فعاله.' : '⛔️ اشتراک غیرفعاله (لغو شده).',
    );
  }
  return lines.join('\n');
}

export function mainMenuKeyboard() {
  return {
    inline_keyboard: [
      [{ text: '⏰ تنظیم ساعت آلارم', callback_data: 'set_hour' }],
      [{ text: '📊 وضعیت من', callback_data: 'status' }],
      [{ text: '🛑 لغو اشتراک', callback_data: 'unsubscribe' }],
    ],
  };
}

export function hourPickerKeyboard() {
  // 3 per row, 06..23.
  const rows = [];
  for (let i = 0; i < HOURS.length; i += 3) {
    rows.push(
      HOURS.slice(i, i + 3).map((h) => ({
        text: `${String(h).padStart(2, '0')}:۰۰`,
        callback_data: `hour:${h}`,
      })),
    );
  }
  rows.push([{ text: '‹ برگشت', callback_data: 'main' }]);
  return { inline_keyboard: rows };
}

export function statusText(subscriber) {
  if (subscriber.alarm_hour == null) {
    return 'هنوز ساعت آلارمی تنظیم نکردی.';
  }
  const state = subscriber.active ? '✅ فعال' : '⛔️ غیرفعال';
  return [
    `⏰ ساعت آلارم: ${String(subscriber.alarm_hour).padStart(2, '0')}:۰۰ (به وقت ایران)`,
    `وضعیت اشتراک: ${state}`,
  ].join('\n');
}

export function backKeyboard() {
  return { inline_keyboard: [[{ text: '‹ برگشت', callback_data: 'main' }]] };
}
