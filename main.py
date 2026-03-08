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
        logger.info(f"✅ FFmpeg найден в системе: {ffmpeg_path}")
        FFMPEG_PATH = ffmpeg_path
        return ffmpeg_path
    
    # Проверяем локальную папку
    if sys.platform == "win32":
        local_ffmpeg = "ffmpeg.exe"
    else:
        local_ffmpeg = "ffmpeg"
    
    if os.path.exists(local_ffmpeg) and os.access(local_ffmpeg, os.X_OK):
        logger.info(f"✅ FFmpeg найден локально: {local_ffmpeg}")
        FFMPEG_PATH = os.path.abspath(local_ffmpeg)
        return FFMPEG_PATH
    
    # Пытаемся скачать
    if not AIOHTTP_AVAILABLE:
        logger.error("❌ FFmpeg не найден и aiohttp не установлен")
        return None
    
    logger.info("⏳ FFmpeg не найден, скачиваю статическую сборку...")
    
    try:
        # Используем statically compiled ffmpeg для Linux
        if sys.platform == "linux":
            # Универсальная сборка для Linux x86_64
            ffmpeg_url = "https://github.com/ffbinaries/ffbinaries-prebuilt/releases/download/v4.4/ffmpeg-4.4-linux-64.zip"
            archive_name = "ffmpeg.zip"
        elif sys.platform == "win32":
            ffmpeg_url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
            archive_name = "ffmpeg.zip"
        else:
            logger.error(f"❌ Платформа {sys.platform} не поддерживается")
            return None
        
        async with aiohttp.ClientSession() as session:
            async with session.get(ffmpeg_url, timeout=300) as response:
                if response.status != 200:
                    logger.error(f"❌ Ошибка загрузки FFmpeg: {response.status}")
                    return None
                
                logger.info("📦 Загрузка FFmpeg...")
                data = await response.read()
                
                # Сохраняем архив
                with open(archive_name, "wb") as f:
                    f.write(data)
                
                logger.info("📦 Архив загружен, распаковываю...")
                
                # Распаковываем
                with zipfile.ZipFile(archive_name, 'r') as zip_ref:
                    zip_ref.extractall("ffmpeg_tmp")
                
                # Ищем файл ffmpeg
                found = False
                for root, dirs, files in os.walk("ffmpeg_tmp"):
                    for file in files:
                        if file.startswith("ffmpeg") and (file.endswith(".exe") or sys.platform != "win32"):
                            src = os.path.join(root, file)
                            shutil.copy(src, local_ffmpeg)
                            if sys.platform != "win32":
                                os.chmod(local_ffmpeg, 0o755)
                            found = True
                            break
                    if found:
                        break
                
                # Удаляем временные файлы
                shutil.rmtree("ffmpeg_tmp")
                os.remove(archive_name)
                
                if found and os.path.exists(local_ffmpeg):
                    FFMPEG_PATH = os.path.abspath(local_ffmpeg)
                    logger.info(f"✅ FFmpeg загружен: {FFMPEG_PATH}")
                    return FFMPEG_PATH
                else:
                    logger.error("❌ Не удалось найти файл ffmpeg в архиве")
                    return None
                    
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки FFmpeg: {type(e).__name__}: {e}", exc_info=True)
    
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
async def convert_to_circle(input_path: str, output_path: str, ffmpeg_path: str = None, max_duration: int = 60) -> bool:
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

    # Команда FFmpeg для создания кружка
    # Telegram требует: MP4, H.264, 640x640, до 60 сек, без аудио, < 1 МБ
    cmd = [
        ffmpeg_path,
        "-i", input_path,
        "-vf", "scale=640:640:force_original_aspect_ratio=increase,crop=640:640",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "28",  # Больше = меньше качество, но меньше размер
        "-pix_fmt", "yuv420p",
        "-t", str(max_duration),
        "-an",
        "-movflags", "+faststart",
        "-y",
        output_path
    ]

    try:
        logger.info(f"🔄 FFmpeg команда: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)

        if process.returncode == 0:
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                logger.info(f"✅ Конвертация успешна! Размер: {size_mb:.2f} МБ")
                
                # Если файл больше 1 МБ, пробуем сжать сильнее
                if size_mb > 1.0:
                    logger.info("⚠️ Файл больше 1 МБ, пробуем сжатие...")
                    cmd_compress = [
                        ffmpeg_path,
                        "-i", output_path,
                        "-c:v", "libx264",
                        "-preset", "slow",
                        "-crf", "30",
                        "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart",
                        "-y",
                        output_path + ".compressed"
                    ]
                    
                    process_compress = await asyncio.create_subprocess_exec(
                        *cmd_compress,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    
                    stdout_c, stderr_c = await asyncio.wait_for(process_compress.communicate(), timeout=120)
                    
                    if process_compress.returncode == 0 and os.path.exists(output_path + ".compressed"):
                        os.remove(output_path)
                        os.rename(output_path + ".compressed", output_path)
                        new_size = os.path.getsize(output_path) / (1024 * 1024)
                        logger.info(f"✅ Сжатие успешно! Новый размер: {new_size:.2f} МБ")
                
                return True
            else:
                logger.error("❌ Выходной файл пуст или не создан")
                return False

        error_msg = stderr.decode('utf-8', errors='ignore')
        logger.error(f"❌ FFmpeg ошибка (код {process.returncode}): {error_msg[:800]}")
        return False

    except asyncio.TimeoutError:
        logger.error("❌ Таймаут FFmpeg (>120 сек)")
        if process:
            process.kill()
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка конвертации: {type(e).__name__}: {e}")
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

    # Уникальные имена временных файлов
    temp_in = f"temp_in_{message.from_user.id}_{int(asyncio.get_event_loop().time())}.mp4"
    temp_out = f"temp_out_{message.from_user.id}_{int(asyncio.get_event_loop().time())}.mp4"

    status = await message.answer("⏳ Конвертирую видео в кружок...")

    try:
        logger.info(f"📥 Скачивание видео: {video.file_id}")
        await bot.download_file(file.file_path, temp_in)
        
        input_size = os.path.getsize(temp_in)
        logger.info(f"✅ Видео скачано: {temp_in} ({input_size / (1024*1024):.2f} МБ)")

        success = await convert_to_circle(temp_in, temp_out, FFMPEG_PATH)

        if success and os.path.exists(temp_out):
            output_size = os.path.getsize(temp_out)
            logger.info(f"📤 Отправка кружка: {temp_out} ({output_size / (1024*1024):.2f} МБ)")
            
            # Получаем длительность из выходного файла
            actual_duration = min(video.duration or 60, 60)
            
            with open(temp_out, 'rb') as f:
                await bot.send_video_note(
                    chat_id=message.chat.id,
                    video_note=f.read(),
                    length=640,
                    duration=actual_duration
                )
            logger.info("✅ Кружок отправлен")
        else:
            await message.answer(
                "❌ Не удалось создать кружок.\n\n"
                "📋 Проверьте консоль бота для подробной ошибки."
            )

    except Exception as e:
        logger.error(f"Ошибка обработки видео: {type(e).__name__}: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {type(e).__name__}\n\n{str(e)[:200]}")

    finally:
        # Очистка временных файлов
        for f in [temp_in, temp_out]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                    logger.info(f"🧹 Удалён временный файл: {f}")
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось удалить {f}: {e}")
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
    logger.info(f"🖥️ Платформа: {sys.platform}")

    if LOGO_AVAILABLE:
        logger.info(f"✅ Логотип готов: {logo_image.size}")
    else:
        logger.warning("⚠️ Логотип не загружен")

    # Проверяем/загружаем FFmpeg
    logger.info("🔍 Проверка FFmpeg...")
    ffmpeg_path = await ensure_ffmpeg()
    if ffmpeg_path:
        logger.info(f"✅ FFmpeg готов: {ffmpeg_path}")
    else:
        logger.error("❌ FFmpeg недоступен — кружки не будут работать")
        logger.error("💡 Совет: установите ffmpeg в системе или загрузите в папку с ботом")

    asyncio.create_task(cleanup_old_albums())

    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("⏹️ Бот остановлен")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
