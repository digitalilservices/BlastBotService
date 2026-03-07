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


async def spam_worker(user_dir, stop_flag, progress_cb):
    settings = json.load(open(f"{user_dir}/settings.json"))
    message_data = json.load(
        open(f"{user_dir}/message.json", encoding="utf-8")
    )
    sessions_dir = f"{user_dir}/sessions"

    delay_groups = settings["delay_between_groups"]
    groups_per_account = settings["groups_per_account"]
    delay_cycle = settings["delay_between_cycles"]

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

            acc_name = sess.replace(".session", "")

            client = TelegramClient(
                f"{sessions_dir}/{acc_name}",
                API_ID,
                API_HASH,
                device_model="Samsung Galaxy S21",
                system_version="Android 13",
                app_version="9.6.3",
                lang_code="ru",
                system_lang_code="ru"
            )

            sent_from_account = 0

            try:
                await client.connect()

                if not await client.is_user_authorized():
                    continue

                async for dialog in client.iter_dialogs(limit=500):
                    if stop_flag["stop"]:
                        break

                    if sent_from_account >= groups_per_account:
                        break

                    if not (dialog.is_group or dialog.is_channel):
                        continue

                    try:
                        chat = await client.get_entity(dialog.id)
                        chat_name = (dialog.name or "").lower()
                        chat_about = (getattr(chat, "about", "") or "").lower()

                        if any(k in chat_name for k in blacklist_keywords) or \
                           any(k in chat_about for k in blacklist_keywords):
                            continue

                        # 📨 FORWARD
                        if message_data["type"] == "forward":
                            await client.forward_messages(
                                dialog.id,
                                message_data["message_id"],
                                message_data["from_chat_id"]
                            )
                        # ✍️ TEXT
                        else:
                            await client.send_message(
                                dialog.id,
                                message_data["text"]
                            )

                        sent += 1
                        sent_from_account += 1

                        await progress_cb(sent, errors_count)

                        await asyncio.sleep(
                            random.randint(delay_groups, delay_groups + 10)
                        )

                    except (
                        errors.ChatWriteForbiddenError,
                        errors.ChannelPrivateError,
                        errors.UserBannedInChannelError
                    ):
                        continue

                    except errors.FloodWaitError as e:
                        await asyncio.sleep(e.seconds)
                        continue

                    except errors.ChatWriteForbiddenError:
                        continue

                    except errors.ChannelPrivateError:
                        continue

                    except errors.UserBannedInChannelError:
                        continue

                    except Exception as e:
                        errors_count += 1
                        print("SEND ERROR:", e)
                        continue

            except Exception:
                errors_count += 1
                await progress_cb(
                    sent,
                    errors_count,
                    {
                        "phone": acc_name,
                        "reason": "error"
                    }
                )

            finally:
                try:
                    await client.disconnect()
                except:
                    pass

        if not stop_flag["stop"]:
            await asyncio.sleep(delay_cycle)

    return sent, errors_count




















