"""Turning an HTTP failure into something Claude can act on. `04-mcp-server.md` §9.

"Errors instruct." An error tells Claude what went wrong *and what to do
next*: "Dataset not found" is a dead end, while naming the closest matches and
the tool that would find more lets the conversation continue.

The API already writes good messages — permission errors name the owner, CRS
errors name the missing `.prj`. This module's job is to pass them through
intact and add the *next step*, which is client-side knowledge: only this
server knows what tools exist.
"""

from typing import Any

import httpx

#: What to suggest for each status. The API supplies what happened; this
#: supplies what to do about it, in terms of the tool surface.
_NEXT_STEP = {
    401: (
        "Sign-in failed or the session expired. This is an authentication "
        "problem on this workstation, not a permission problem with the data."
    ),
    403: (
        "Ask the owner named above to grant access, then try again. Do not "
        "retry without that — permissions are resolved live, so the answer "
        "will not change on its own."
    ),
    404: (
        "Use webmap_search_datasets with a broader term to find what is "
        "available. Note that a dataset owned by someone else and not shared "
        "is indistinguishable from one that does not exist."
    ),
    409: "Someone else changed this object first. Re-read it and try again.",
    413: "Split the input or use a smaller extent.",
    422: (
        "The file could not be read as described. The message above says what "
        "is missing; supplying it on re-import usually resolves it."
    ),
    429: (
        "A quota is exhausted. The message says which and when it resets — "
        "wait rather than retrying immediately."
    ),
    500: (
        "This is a WebMap bug, not a problem with the request. Retrying the "
        "same call will not help; report it with the request id if one is "
        "shown."
    ),
    502: "WebMap is unreachable or restarting. Retry in a few seconds.",
    503: "WebMap is unreachable or restarting. Retry in a few seconds.",
}


def describe_http_error(response: httpx.Response) -> str:
    """Compose the message a tool raises when the API refuses.

    Keeps the API's own wording rather than paraphrasing it: those messages
    name owners, missing files, and limits, and a summary would lose exactly
    the parts that make them actionable.
    """
    detail = _detail(response)
    step = _NEXT_STEP.get(response.status_code)
    if step is None:
        step = (
            "Unexpected response from WebMap. Retrying is unlikely to help."
            if response.status_code >= 500
            else "Check the request parameters against the tool description."
        )

    request_id = response.headers.get("x-request-id")
    trailer = f" (request {request_id})" if request_id else ""
    return f"{detail}\n\n{step}{trailer}"


def _detail(response: httpx.Response) -> str:
    """Pull the API's message out, or say plainly that there was not one."""
    try:
        payload: Any = response.json()
    except ValueError:
        return f"WebMap returned HTTP {response.status_code} with no readable body."

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        # FastAPI validation errors are a list of per-field objects. Flattened
        # rather than dumped, because the raw shape is noise in a conversation.
        if isinstance(detail, list):
            parts = [
                f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}"
                for item in detail
                if isinstance(item, dict)
            ]
            if parts:
                return "Invalid request — " + "; ".join(parts)
    return f"WebMap returned HTTP {response.status_code}."
