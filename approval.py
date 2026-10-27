"""
Approval tokens: proof that a person said yes to one exact write action.

The agent loop and the MCP server share a secret (JOBSEARCH_APPROVAL_KEY). After
a person approves an action, the loop mints a token over that exact tool and
those exact arguments and sends it in the request's `_meta`, which the model
never sees. The server refuses a risky tool call unless the token verifies.

A token is:
  * bound to the tool name and the canonical form of its arguments, so it
    cannot approve anything other than what was shown;
  * short-lived (TOKEN_TTL_SECONDS);
  * single-use: the server remembers nonces it has accepted.

This guards against mistakes (a buggy agent, a model that calls a risky tool
directly, another MCP client with no approval step). It does not guard against
code that can read the secret, which on one machine is an acceptable limit.
"""
import hashlib
import hmac
import json
import os
import secrets
import time

KEY_ENV = "JOBSEARCH_APPROVAL_KEY"
META_FIELD = "approval_token"
TOKEN_TTL_SECONDS = 120


def new_key():
    return secrets.token_hex(32)


def canonical(tool, arguments):
    """The exact bytes a token covers. None values are dropped, keys sorted."""
    args = {k: v for k, v in (arguments or {}).items() if v is not None}
    return tool + "\n" + json.dumps(args, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False)


def _sign(key, message):
    return hmac.new(bytes.fromhex(key), message.encode("utf-8"), hashlib.sha256).hexdigest()


def mint(key, tool, arguments, now=None):
    expires = int((now or time.time()) + TOKEN_TTL_SECONDS)
    nonce = secrets.token_hex(8)
    sig = _sign(key, f"{canonical(tool, arguments)}\n{expires}\n{nonce}")
    return f"{expires}.{nonce}.{sig}"


class Verifier:
    """Server side. One instance per server process, so used nonces are remembered."""

    def __init__(self, key=None):
        self.key = key if key is not None else os.environ.get(KEY_ENV)
        self._used = set()

    def check(self, token, tool, arguments, now=None):
        """Return None if the token approves this exact call, else the reason it does not."""
        if not self.key:
            return ("approvals are not configured on this server, so risky tools are "
                    "disabled. Run them through agent.py, which asks a person first.")
        if not token:
            return "missing approval token"
        try:
            expires_s, nonce, sig = token.split(".")
            expires = int(expires_s)
        except ValueError:
            return "malformed approval token"
        expected = _sign(self.key, f"{canonical(tool, arguments)}\n{expires}\n{nonce}")
        if not hmac.compare_digest(sig, expected):
            return "approval token does not match this tool and these arguments"
        if (now or time.time()) > expires:
            return "approval token expired"
        if nonce in self._used:
            return "approval token already used"
        self._used.add(nonce)
        return None
