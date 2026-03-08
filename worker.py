import asyncio
import os
import json
import random
from telethon import TelegramClient, errors
from config import API_ID, API_HASH


blacklist_keywords = [
    "запрещена реклама",
    "реклама запрещена",
    "no ads",
    "без рекламы",
    "no advertising",
]


def _reason_text(exc: Exception) -> str:
    if isinstance(exc, errors.FloodWaitError):
        return f"flood_{exc.seconds}s"

    if isinstance(exc, errors.ChatWriteForbiddenError):
        return "no_write_permission"

    if isinstance(exc, errors.ChannelPrivateError):
        return "channel_private"

    if isinstance(exc, errors.UserBannedInChannelError):
        return "banned_in_chat"

    if isinstance(exc, errors.SlowModeWaitError):
        return f"slowmode_{exc.seconds}s"

    return exc.__class__.__name__.lower()


def create_worker_client(session_path: str) -> TelegramClient:
    return TelegramClient(
        session_path,
        API_ID,
        API_HASH,
        device_model="Samsung Galaxy S21",
        system_version="Android 13",
        app_version="9.6.3",
        lang_code="ru",
        system_lang_code="ru"
    )


async def spam_worker(user_dir, stop_flag, progress_cb):
    with open(f"{user_dir}/settings.json", "r", encoding="utf-8") as f:
        settings = json.load(f)

    with open(f"{user_dir}/message.json", "r", encoding="utf-8") as f:
        message_data = json.load(f)

    sessions_dir = f"{user_dir}/sessions"

    delay_groups = int(settings["delay_between_groups"])
    groups_per_account = int(settings["groups_per_account"])
    delay_cycle = int(settings["delay_between_cycles"])

    sent = 0
    errors_count = 0

    while not stop_flag["stop"]:
        session_files = [
            f for f in os.listdir(sessions_dir)
            if f.endswith(".session")
        ]
        random.shuffle(session_files)

        for sess in session_files:
            if stop_flag["stop"]:
                break

            acc_name = sess[:-8]  # remove ".session"
            session_path = f"{sessions_dir}/{acc_name}"
            client = create_worker_client(session_path)

            sent_from_account = 0

            try:
                await client.connect()

                if not await client.is_user_authorized():
                    errors_count += 1
                    await progress_cb(
                        sent,
                        errors_count,
                        {
                            "phone": acc_name,
                            "reason": "not_authorized"
                        }
                    )
                    continue

                async for dialog in client.iter_dialogs(limit=300):
                    if stop_flag["stop"]:
                        break

                    if sent_from_account >= groups_per_account:
                        break

                    try:
                        entity = dialog.entity

                        is_broadcast = bool(getattr(entity, "broadcast", False))
                        is_megagroup = bool(getattr(entity, "megagroup", False))
                        is_group = bool(dialog.is_group)

                        # пропускаем обычные каналы-вещатели
                        if is_broadcast:
                            continue

                        # оставляем только группы / супергруппы
                        if not (is_group or is_megagroup):
                            continue

                        chat_name = (dialog.name or "").lower()
                        chat_about = (getattr(entity, "about", "") or "").lower()

                        if any(k in chat_name for k in blacklist_keywords):
                            continue

                        if any(k in chat_about for k in blacklist_keywords):
                            continue

                        if message_data.get("type") == "forward":
                            await client.forward_messages(
                                dialog.id,
                                message_data["message_id"],
                                message_data["from_chat_id"]
                            )
                        else:
                            text = (message_data.get("text") or "").strip()
                            if not text:
                                continue

                            await client.send_message(dialog.id, text)

                        sent += 1
                        sent_from_account += 1

                        await progress_cb(sent, errors_count)

                        await asyncio.sleep(
                            random.randint(delay_groups, delay_groups + 5)
                        )

                    except errors.FloodWaitError as e:
                        errors_count += 1
                        await progress_cb(
                            sent,
                            errors_count,
                            {
                                "phone": acc_name,
                                "reason": f"flood_{e.seconds}s"
                            }
                        )
                        await asyncio.sleep(e.seconds)
                        continue

                    except (
                        errors.ChatWriteForbiddenError,
                        errors.ChannelPrivateError,
                        errors.UserBannedInChannelError,
                        errors.SlowModeWaitError,
                    ) as e:
                        errors_count += 1
                        await progress_cb(
                            sent,
                            errors_count,
                            {
                                "phone": acc_name,
                                "reason": _reason_text(e)
                            }
                        )
                        continue

                    except Exception as e:
                        errors_count += 1
                        print(f"SEND ERROR [{acc_name}] -> {dialog.id}: {repr(e)}")
                        await progress_cb(
                            sent,
                            errors_count,
                            {
                                "phone": acc_name,
                                "reason": _reason_text(e)
                            }
                        )
                        continue

            except Exception as e:
                errors_count += 1
                print(f"ACCOUNT ERROR [{acc_name}]: {repr(e)}")
                await progress_cb(
                    sent,
                    errors_count,
                    {
                        "phone": acc_name,
                        "reason": _reason_text(e)
                    }
                )

            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        if not stop_flag["stop"]:
            await asyncio.sleep(delay_cycle)

    return sent, errors_count
