"""Whitelist shared by the music plugin and the local ncm-cli bridge.

Only read-only ncm-cli commands are allowed. The bridge is deliberately not a
generic command runner: adding a write command here would expose the owner's
account to every caller that has the bridge token.
"""
from __future__ import annotations

READ_ONLY_COMMANDS: frozenset[tuple[str, str]] = frozenset({
    # search
    ("search", "song"),
    ("search", "album"),
    ("search", "playlist"),
    ("search", "mv"),
    ("search", "podcast"),
    ("search", "voice"),
    ("search", "all"),
    # user / personal data (owner-only at the plugin layer)
    ("user", "info"),
    ("user", "favorite"),
    ("user", "history"),
    ("user", "listen-ranking"),
    ("user", "album-history"),
    ("user", "playlist-history"),
    ("user", "radio-history"),
    ("user", "voice-history"),
    ("user", "voicebook-history"),
    # playlists
    ("playlist", "created"),
    ("playlist", "collected"),
    ("playlist", "get"),
    ("playlist", "tracks"),
    ("playlist", "getTags"),
    ("playlist", "radar"),
    # albums / artists
    ("album", "collected"),
    ("album", "get"),
    ("album", "tracks"),
    ("artist", "songs"),
    # recommendations
    ("recommend", "daily"),
    ("recommend", "fm"),
    ("recommend", "heartbeat"),
    # lyrics / comments / notes
    ("song", "lyric"),
    ("comment", "list-hot"),
    ("note", "topicSearch"),
    # podcasts
    ("podcast", "categories"),
    ("podcast", "created"),
    ("podcast", "get"),
    ("podcast", "voices"),
    ("podcast", "voiceDetail"),
    # cloud drive
    ("cloud", "list"),
    ("cloud", "listFile"),
    # AI song
    ("aisong", "pointBalance"),
    ("aisong", "list"),
    ("aisong", "detail"),
    ("aisong", "batchDetail"),
})

FORBIDDEN_OPTIONS = frozenset({"--output", "--help", "-h", "--userInput"})
MAX_ARGS = 24
MAX_TOKEN_LENGTH = 200
MAX_LIMIT = 200
MAX_OFFSET = 1_000_000


class ReadonlyError(ValueError):
    pass


def validate_args(args: object) -> list[str]:
    """Return a clean argv list, or raise ReadonlyError.

    The caller still runs the command with ``subprocess`` and a list (never a
    shell), so this is an allowlist on top of argv separation, not a shell
    escaping layer.
    """
    if not isinstance(args, list) or len(args) < 2:
        raise ReadonlyError("命令参数必须是至少包含命令和子命令的数组")
    if len(args) > MAX_ARGS:
        raise ReadonlyError(f"命令参数过多，最多 {MAX_ARGS} 个")
    clean: list[str] = []
    for raw in args:
        if not isinstance(raw, str):
            raise ReadonlyError("命令参数必须是字符串")
        token = raw.strip()
        if not token:
            raise ReadonlyError("命令参数不能为空")
        if len(token) > MAX_TOKEN_LENGTH:
            raise ReadonlyError(f"单个参数不能超过 {MAX_TOKEN_LENGTH} 个字符")
        if any(ch in token for ch in ("\x00", "\r", "\n")):
            raise ReadonlyError("命令参数不能包含控制字符")
        if token in FORBIDDEN_OPTIONS:
            raise ReadonlyError(f"不允许参数 {token}")
        clean.append(token)

    command = (clean[0], clean[1])
    if command not in READ_ONLY_COMMANDS:
        raise ReadonlyError(f"只允许只读音乐命令，收到：{' '.join(command)}")

    for index, token in enumerate(clean):
        if token == "--limit" and index + 1 < len(clean):
            _check_int(clean[index + 1], 1, MAX_LIMIT, "--limit")
        elif token == "--offset" and index + 1 < len(clean):
            _check_int(clean[index + 1], 0, MAX_OFFSET, "--offset")
    return clean


def _check_int(value: str, minimum: int, maximum: int, name: str) -> None:
    try:
        number = int(value)
    except ValueError:
        raise ReadonlyError(f"{name} 必须是整数") from None
    if not minimum <= number <= maximum:
        raise ReadonlyError(f"{name} 必须在 {minimum}..{maximum} 之间")
