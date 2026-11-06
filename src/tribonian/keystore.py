"""keystore.py -- the CourtListener API token: every read and write goes through here.

Lookup order:
    1. COURTLISTENER_TOKEN (environment)
    2. the system key store, through the `keyring` library: Windows Credential Manager, macOS Keychain,
       Linux Secret Service (the store behind secret-tool)
    3. an older Linux entry stored with `secret-tool store ... service courtlistener` (read only, so nobody
       has to paste the token again)
    4. courtlistener_token in the config file

store() saves to the key store and removes any copy in the config file. When no key store works it
saves to the config file instead (readable only by the owner where the system supports that) and
says so. skip() records that the user chose to go without a token, which a missing token alone
can't show; storing a token clears it.
"""
import os, shutil, subprocess

from tribonian import tconfig

SERVICE, USER = "tribonian-us", "courtlistener"
SKIP_KEY = "token_skipped"


def _keyring():
    """The keyring module when it has a working backend, else None."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        kr = keyring.get_keyring()
    except Exception:
        return None
    if isinstance(kr, fail.Keyring) or "fail" in type(kr).__module__ or "null" in type(kr).__module__:
        return None
    return keyring


def _legacy():
    if not shutil.which("secret-tool"):
        return ""
    try:
        return subprocess.run(["secret-tool", "lookup", "service", "courtlistener"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def lookup():
    """(token, where it came from) or ("", "")."""
    t = os.environ.get("COURTLISTENER_TOKEN", "").strip()
    if t:
        return t, "COURTLISTENER_TOKEN"
    kr = _keyring()
    if kr:
        try:
            t = (kr.get_password(SERVICE, USER) or "").strip()
        except Exception:
            t = ""
        if t:
            return t, "system key store"
    t = _legacy()
    if t:
        return t, "secret-tool (service courtlistener)"
    t = str(tconfig.FILE.get("courtlistener_token") or "").strip()
    if t:
        return t, "config file %s" % tconfig.config_path()
    return "", ""


def skipped():
    return bool(tconfig.FILE.get(SKIP_KEY))


def answered():
    """The token question is settled: a token is present, or the user chose Skip."""
    return bool(lookup()[0]) or skipped()


def _write(key, value):
    from tribonian import setup_guide
    setup_guide.write_setting(tconfig.config_path(), key, value)
    if value in ("", None):
        tconfig.FILE.pop(key, None)
    else:
        tconfig.FILE[key] = value


def store(token):
    """Save the token; returns a sentence saying where (never the token)."""
    token = token.strip()
    if not token:
        raise ValueError("empty token")
    kr = _keyring()
    saved = False
    if kr:
        try:
            kr.set_password(SERVICE, USER, token)
            saved = kr.get_password(SERVICE, USER) == token
        except Exception:
            saved = False
    if SKIP_KEY in tconfig.FILE:
        _write(SKIP_KEY, "")
    if saved:
        if "courtlistener_token" in tconfig.FILE:
            _write("courtlistener_token", "")
        return "saved in the system key store"
    _write("courtlistener_token", token)
    return ("no system key store works here, so the token is saved in %s%s. Don't share or commit that file."
            % (tconfig.config_path(), ", readable only by you" if os.name == "posix" else ""))


def skip():
    _write(SKIP_KEY, True)
    return "skipped: no CourtListener requests until a token is added"


def forget():
    """Remove the token from the key store and the config file (not COURTLISTENER_TOKEN or the secret-tool entry)."""
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(SERVICE, USER)
        except Exception:
            pass
    if "courtlistener_token" in tconfig.FILE:
        _write("courtlistener_token", "")
