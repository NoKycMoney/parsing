async def _resolve_valid_chats(client: TelegramClient, raw_chats: list) -> list:
    valid = []
    failed = []
    total = len(raw_chats)

    for idx, ch in enumerate(raw_chats, start=1):
        try:
            entity = await client.get_entity(ch)
            # Дополнительно проверяем возможность прочитать последнее сообщение
            async for _ in client.iter_messages(entity, limit=1):
                break
            valid.append(ch)
            title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(ch)
            print(f"[{idx}/{total}] OK @{str(ch).lstrip('@')} ({title})", flush=True)
        except Exception as e:
            failed.append((ch, f"{type(e).__name__}: {e}"))
            print(f"[{idx}/{total}] FAIL @{str(ch).lstrip('@')} -> {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(0.15)

    _state["failed_channels"] = [str(c) for c, _ in failed]
    print(f"Проверка каналов завершена: OK {len(valid)}/{total}, ошибок: {len(failed)}", flush=True)
    if failed:
        bad_list = ", ".join(f"@{str(c).lstrip('@')}" for c, _ in failed)
        print(f"Проблемные каналы: {bad_list}", flush=True)

    return valid
