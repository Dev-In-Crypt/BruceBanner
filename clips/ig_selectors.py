"""Every Instagram selector and health text lives here and nowhere else (the UI changes often).

Lists are alternatives (English and Russian UI); the first visible one wins.
These are starting points: verify them in dry-run screenshots and fix them only here.
"""

# ---- health signals (lowercase; matched against URL and page text) ----
URL_LOGIN = ("/accounts/login",)
URL_CHECKPOINT = ("/challenge", "/accounts/suspended", "/auth_platform")
URL_DISABLED = ("/accounts/disabled",)
TEXT_DISABLED = ("your account has been disabled", "we disabled your account",
                 "ваш аккаунт отключен", "мы отключили ваш аккаунт")
TEXT_CHECKPOINT = ("confirm it's you", "enter the code", "we sent a code", "upload a selfie",
                   "подтвердите, что это вы", "введите код", "мы отправили код", "селфи")
TEXT_ACTION_BLOCK = ("try again later", "we restrict certain activity",
                     "повторите попытку позже", "мы ограничиваем")
LOGGED_IN = ['svg[aria-label="Home"]', 'svg[aria-label="Главная"]']

# ---- new post flow ----
CREATE = ['svg[aria-label="New post"]', 'svg[aria-label="Новая публикация"]',
          'svg[aria-label="Create"]', 'svg[aria-label="Создать"]']
CREATE_POST_ITEM = ['a:has(svg[aria-label="Post"])', 'a:has(svg[aria-label="Публикация"])',
                    'div[role="button"]:has-text("Post")', 'div[role="button"]:has-text("Публикация")']
FILE_INPUT = ['div[role="dialog"] input[type="file"]', 'input[type="file"]']
REELS_NOTICE_OK = ['div[role="dialog"] button:has-text("OK")', 'div[role="dialog"] button:has-text("ОК")']
CROP_BUTTON = ['svg[aria-label="Select crop"]', 'svg[aria-label="Выбрать размер и обрезать"]']
CROP_ORIGINAL = ['div[role="button"]:has-text("Original")', 'div[role="button"]:has-text("Оригинал")']
NEXT = ['div[role="dialog"] div[role="button"]:text-is("Next")',
        'div[role="dialog"] div[role="button"]:text-is("Далее")']
CAPTION = ['div[role="dialog"] div[contenteditable="true"][role="textbox"]']
SHARE = ['div[role="dialog"] div[role="button"]:text-is("Share")',
         'div[role="dialog"] div[role="button"]:text-is("Поделиться")']
SHARED_OK = ['text=/reel has been shared/i', 'text=/post has been shared/i',
             'text=/опубликован/i', 'text=/поделились/i']
CANCEL_POST = ['div[role="dialog"] button:has-text("Discard")', 'div[role="dialog"] button:has-text("Отменить")']
REEL_LINK = 'a[href*="/reel/"]'
