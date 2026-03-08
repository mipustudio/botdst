import os
import sys
import asyncio
import logging
import shutil
import zipfile
from io import BytesIO
from collections import defaultdict
from datetime import datetime

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

# ========== НАСТРОЙКА ЛОГГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ========== КОНФИГУРАЦИЯ ==========
class Config:
    # Получаем токен из переменных окружения Bothost
    TOKEN = os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
    if not TOKEN:
        logger.error("❌ Не найден BOT_TOKEN в переменных окружения!")
        raise ValueError("Установите BOT_TOKEN в настройках Bothost")
    
    # Переменные Bothost
    BOT_ID = os.getenv('BOT_ID', '')
    USER_ID = os.getenv('USER_ID', '')
    
    # Настройки обработки фото
    MAX_PHOTOS_PER_BATCH = 10
    LOGO_SCALE = 0.15
    LOGO_PADDING = 20
    
    @staticmethod
    def get_agent_url():
        """URL API Bothost"""
        return os.getenv('BOTHOST_AGENT_URL', 'http://agent:8000')

config = Config()
logger.info(f"✅ Конфигурация загружена")

# Глобальная переменная для пути к FFmpeg
FFMPEG_PATH = None

# ========== АВТОЗАГРУЗКА FFMPEG ==========
async def ensure_ffmpeg() -> str:
    """Проверяет наличие ffmpeg, при отсутствии скачивает"""
    global FFMPEG_PATH
    
    # Сначала проверяем систему
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        logger.info(f"✅ FFmpeg найден: {ffmpeg_path}")
        FFMPEG_PATH = ffmpeg_path
        return ffmpeg_path
    
    # Проверяем локальную папку
    if sys.platform == "win32":
        local_ffmpeg = "ffmpeg.exe"
    else:
        local_ffmpeg = "ffmpeg"
    
    if os.path.exists(local_ffmpeg):
        logger.info(f"✅ FFmpeg найден локально: {local_ffmpeg}")
        FFMPEG_PATH = local_ffmpeg
        return local_ffmpeg
    
    # Пытаемся скачать
    if not AIOHTTP_AVAILABLE:
        logger.error("❌ FFmpeg не найден и aiohttp не установлен")
        return None
    
    logger.info("⏳ FFmpeg не найден, скачиваю...")
    
    try:
        # Ссылки на статические билды FFmpeg
        if sys.platform == "win32":
            ffmpeg_url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        elif sys.platform == "linux":
            ffmpeg_url = "https://johnvansickle.com/ffmpeg/builds/ffmpeg-git-amd64-static.tar.xz"
        else:
            logger.error(f"❌ Платформа {sys.platform} не поддерживается для автозагрузки")
            return None
        
        async with aiohttp.ClientSession() as session:
            async with session.get(ffmpeg_url, timeout=300) as response:
                if response.status != 200:
                    logger.error(f"❌ Ошибка загрузки FFmpeg: {response.status}")
                    return None
                
                data = await response.read()
                
                # Сохраняем архив
                archive_name = "ffmpeg_archive.zip" if sys.platform == "win32" else "ffmpeg_archive.tar.xz"
                with open(archive_name, "wb") as f:
                    f.write(data)
                
                logger.info("📦 Архив загружен, распаковываю...")
                
                # Распаковываем
                if sys.platform == "win32":
                    with zipfile.ZipFile(archive_name, 'r') as zip_ref:
                        zip_ref.extractall("ffmpeg_extracted")
                    
                    # Ищем exe файл
                    for root, dirs, files in os.walk("ffmpeg_extracted"):
                        for file in files:
                            if file.endswith("ffmpeg.exe"):
                                src = os.path.join(root, file)
                                shutil.copy(src, local_ffmpeg)
                                break
                    shutil.rmtree("ffmpeg_extracted")
                else:
                    import tarfile
                    with tarfile.open(archive_name, 'r:xz') as tar_ref:
                        tar_ref.extractall("ffmpeg_extracted")
                    
                    for root, dirs, files in os.walk("ffmpeg_extracted"):
                        for file in files:
                            if file == "ffmpeg":
                                src = os.path.join(root, file)
                                shutil.copy(src, local_ffmpeg)
                                os.chmod(local_ffmpeg, 0o755)
                                break
                    shutil.rmtree("ffmpeg_extracted")
                
                # Удаляем архив
                os.remove(archive_name)
                
                if os.path.exists(local_ffmpeg):
                    logger.info(f"✅ FFmpeg загружен: {local_ffmpeg}")
                    FFMPEG_PATH = local_ffmpeg
                    return local_ffmpeg
                    
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки FFmpeg: {e}")
    
    return None

# ========== ХРАНЕНИЕ АЛЬБОМОВ ==========
album_storage = defaultdict(list)

# ========== ЗАГРУЗКА ЛОГОТИПА ==========
PIL_AVAILABLE = False
LOGO_AVAILABLE = False
logo_image = None

try:
    from PIL import Image
    PIL_AVAILABLE = True
    
    if os.path.exists("logo.png"):
        try:
            logo_image = Image.open("logo.png")
            if logo_image.mode != 'RGBA':
                logo_image = logo_image.convert('RGBA')
            LOGO_AVAILABLE = True
            logger.info(f"✅ Логотип загружен: {logo_image.size}")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки логотипа: {e}")
    else:
        logger.warning("⚠️ Файл logo.png не найден")
        
except ImportError:
    logger.warning("⚠️ Pillow не установлен")

# ========== ИНИЦИАЛИЗАЦИЯ БОТА ==========
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InputMediaPhoto, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

bot = Bot(token=config.TOKEN)
dp = Dispatcher()

# ========== СОСТОЯНИЯ ==========
class UserState(StatesGroup):
    waiting_for_action = State()
    processing_video = State()

# ========== ФУНКЦИИ ОБРАБОТКИ ФОТО ==========
async def apply_watermark_bytes(photo_bytes: bytes) -> BytesIO:
    """Накладывает логотип на фото в памяти"""
    if not LOGO_AVAILABLE or not PIL_AVAILABLE:
        raise ValueError("Логотип не загружен")
    
    user_image = Image.open(BytesIO(photo_bytes)).convert('RGBA')
    logo_width = int(user_image.width * config.LOGO_SCALE)
    logo_height = int(logo_image.height * (logo_width / logo_image.width))
    resized_logo = logo_image.resize((logo_width, logo_height), Image.Resampling.LANCZOS)
    
    x = user_image.width - logo_width - config.LOGO_PADDING
    y = config.LOGO_PADDING
    
    result_image = user_image.copy()
    result_image.paste(resized_logo, (x, y), resized_logo)
    
    output = BytesIO()
    result_image.save(output, format='PNG', quality=95)
    output.seek(0)
    
    return output

async def process_single_photo(message: Message):
    """Обработка одного фото"""
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = await bot.download_file(file.file_path)
        processed = await apply_watermark_bytes(photo_bytes.read())
        
        await message.answer_photo(
            photo=BufferedInputFile(processed.getvalue(), filename="watermarked.png"),
            caption="✅ Фото обработано с логотипом!"
        )
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        await message.answer("❌ Ошибка при обработке фото")

async def process_album(messages: list):
    """Обработка альбома"""
    photo_count = len(messages)
    
    if photo_count > config.MAX_PHOTOS_PER_BATCH:
        await messages[0].answer(
            f"❌ Слишком много фото: {photo_count}\n"
            f"Максимум: {config.MAX_PHOTOS_PER_BATCH}"
        )
        return
    
    status_msg = await messages[0].answer(f"🔄 Обрабатываю {photo_count} фото...")
    
    processed_photos = []
    for i, msg in enumerate(messages):
        try:
            photo = msg.photo[-1]
            file = await bot.get_file(photo.file_id)
            photo_bytes = await bot.download_file(file.file_path)
            processed = await apply_watermark_bytes(photo_bytes.read())
            processed_photos.append(processed)
            logger.info(f"✅ Обработано фото {i+1}/{photo_count}")
        except Exception as e:
            logger.error(f"❌ Ошибка фото {i+1}: {e}")
            continue
    
    if not processed_photos:
        await status_msg.edit_text("❌ Не удалось обработать фото")
        return
    
    if len(processed_photos) == 1:
        await messages[0].answer_photo(
            photo=BufferedInputFile(processed_photos[0].getvalue(), "photo.png"),
            caption="✅ Готово!"
        )
    else:
        media_group = []
        for i, processed in enumerate(processed_photos):
            caption = f"📸 {i+1}/{len(processed_photos)}" if i == 0 else ""
            media_group.append(
                InputMediaPhoto(
                    media=BufferedInputFile(processed.getvalue(), f"photo_{i}.png"),
                    caption=caption
                )
            )
        await messages[0].answer_media_group(media_group)
        await messages[0].answer("✅ Альбом обработан!")
    
    await status_msg.delete()

# ========== ФУНКЦИЯ КОНВЕРТАЦИИ ВИДЕО В КРУЖОК ==========
async def convert_to_circle(input_path: str, output_path: str, ffmpeg_path: str = None) -> bool:
    """Конвертирует видео в формат Video Note (кружок)"""

    if not ffmpeg_path:
        ffmpeg_path = shutil.which("ffmpeg")
    
    # Если не найден, пробуем альтернативные пути
    if not ffmpeg_path:
        possible_paths = [
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "./ffmpeg",
            "ffmpeg.exe"
        ]
        for path in possible_paths:
            if os.path.exists(path):
                ffmpeg_path = path
                break

    if not ffmpeg_path:
        logger.error("❌ FFmpeg не найден в системе!")
        return False
    
    if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
        logger.error(f"❌ Проблема с входным файлом: {input_path}")
        return False

    # ✅ ИСПРАВЛЕННАЯ КОМАНДА:
    # - scale с increase: видео увеличивается, чтобы ПОКРЫТЬ 640×640
    # - crop: обрезает центр до точного квадрата
    # - -an: убираем аудио (не нужно для кружков)
    # - -preset fast: быстрее кодирование
    # - -crf 23: баланс качества и размера
    cmd = [
        ffmpeg_path,
        "-i", input_path,
        "-vf", "scale=640:640:force_original_aspect_ratio=increase,crop=640:640",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-t", "60",
        "-an",
        "-movflags", "+faststart",
        "-y",
        output_path
    ]
    
    try:
        logger.info(f"🔄 FFmpeg: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        
        if process.returncode == 0:
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                logger.info(f"✅ Конвертация успешна! Размер: {os.path.getsize(output_path)} байт")
                return True
        
        error_msg = stderr.decode('utf-8', errors='ignore')
        logger.error(f"❌ FFmpeg ошибка: {error_msg[:800]}")
        return False
        
    except asyncio.TimeoutError:
        logger.error("❌ Таймаут FFmpeg (>120 сек)")
        if process:
            process.kill()
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка: {type(e).__name__}: {e}")
        return False

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📸 Обработать фото", callback_data="user_photo")
    builder.button(text="🎥 Сделать кружок", callback_data="user_video")
    builder.button(text="🔄 Меню", callback_data="menu")
    builder.adjust(2, 1)
    return builder.as_markup()

# ========== ХЕНДЛЕРЫ ==========
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(UserState.waiting_for_action)

    text = (
        "🤖 **Бот для обработки медиа готов!**\n\n"
        "📸 **Фото:** Накладываю логотип (до 10 шт)\n"
        "🎥 **Видео:** Делаю кружок (640×640, до 60 сек)\n\n"
        "👇 **Выберите действие:**"
    )

    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_for_action)
    await callback.message.edit_text("Меню:", reply_markup=get_main_keyboard())
    await callback.answer()

# --- ФОТО ---
@dp.message(F.photo)
async def handle_photo(message: Message):
    if not PIL_AVAILABLE or not LOGO_AVAILABLE:
        await message.answer("❌ logo.png не найден")
        return

    if message.media_group_id:
        album_storage[message.media_group_id].append(message)
        await asyncio.sleep(1.5)

        if message.media_group_id in album_storage:
            album = album_storage.pop(message.media_group_id)
            await process_album(album)
    else:
        await process_single_photo(message)

@dp.callback_query(F.data == "user_photo")
async def btn_photo_info(callback: CallbackQuery):
    await callback.message.answer("👇 Отправьте фото или альбом (до 10 шт)")
    await callback.answer()

# --- ВИДЕО ---
@dp.callback_query(F.data == "user_video")
async def start_video_mode(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.processing_video)
    await callback.message.answer("📤 Отправьте видео (до 60 сек)")
    await callback.answer()

@dp.message(F.video, UserState.processing_video)
async def handle_video(message: Message, state: FSMContext):
    video = message.video
    file = await bot.get_file(video.file_id)

    temp_in = f"temp_{video.file_id}.mp4"
    temp_out = f"circle_{video.file_id}.mp4"

    status = await message.answer("⏳ Конвертирую видео в кружок...")

    try:
        logger.info(f"📥 Скачивание видео: {video.file_id}")
        await bot.download_file(file.file_path, temp_in)
        logger.info(f"✅ Видео скачано: {temp_in} ({os.path.getsize(temp_in)} байт)")

        success = await convert_to_circle(temp_in, temp_out, FFMPEG_PATH)

        if success:
            logger.info(f"📤 Отправка кружка: {temp_out}")
            with open(temp_out, 'rb') as f:
                await bot.send_video_note(
                    chat_id=message.chat.id,
                    video_note=BufferedInputFile(f.read(), "circle.mp4"),
                    length=640,
                    duration=min(video.duration or 60, 60)
                )
            logger.info("✅ Кружок отправлен")
        else:
            await message.answer(
                "❌ Не удалось создать кружок.\n\n"
                "📋 Проверьте консоль бота для подробной ошибки."
            )

    except Exception as e:
        logger.error(f"Ошибка обработки видео: {e}")
        await message.answer("❌ Произошла непредвиденная ошибка")

    finally:
        for f in [temp_in, temp_out]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                    logger.info(f"🧹 Удалён временный файл: {f}")
                except:
                    pass
        await status.delete()
        await state.set_state(UserState.waiting_for_action)

@dp.callback_query(F.data == "menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await cmd_start(callback.message, state)
    await callback.answer()

# ========== ОЧИСТКА ==========
async def cleanup_old_albums():
    while True:
        await asyncio.sleep(300)
        try:
            keys = []
            for key in list(album_storage.keys()):
                if album_storage[key]:
                    t = datetime.fromtimestamp(album_storage[key][0].date)
                    if (datetime.now() - t).total_seconds() > 600:
                        keys.append(key)
            for key in keys:
                del album_storage[key]
                logger.info(f"🧹 Очищен альбом: {key}")
        except: pass

# ========== ЗАПУСК ==========
async def main():
    logger.info("🚀 Запускаю Telegram бота...")
    logger.info(f"🤖 Bot ID: {config.BOT_ID or 'не указан'}")

    if LOGO_AVAILABLE:
        logger.info(f"✅ Логотип готов: {logo_image.size}")
    else:
        logger.warning("⚠️ Логотип не загружен")

    # Проверяем/загружаем FFmpeg
    ffmpeg_path = await ensure_ffmpeg()
    if not ffmpeg_path:
        logger.warning("⚠️ FFmpeg недоступен — кружки не будут работать")

    asyncio.create_task(cleanup_old_albums())

    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("⏹️ Бот остановлен")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
