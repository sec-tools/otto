from __future__ import annotations
"""
macOS Keychain credential management.

Stores and retrieves API keys and OAuth tokens securely using
the macOS Keychain via the `keyring` library. Credentials are
NEVER stored in config files or on disk.
"""

import logging

logger = logging.getLogger("otto.utils.credentials")

SERVICE_NAME = "Otto"


def store_credential(account: str, credential: str) -> None:
    """
    Store a credential in the macOS Keychain.

    Args:
        account: Identifier (e.g., 'gmail:user@example.com', 'openai_api_key').
        credential: The secret value to store.
    """
    import keyring
    keyring.set_password(SERVICE_NAME, account, credential)
    logger.info("Stored credential for %s", account, extra={"event_type": "credential_stored"})


def get_credential(account: str) -> str | None:
    """
    Retrieve a credential from the macOS Keychain.

    Args:
        account: Identifier used when storing.

    Returns:
        The credential string, or None if not found.
    """
    import keyring
    value = keyring.get_password(SERVICE_NAME, account)
    if value:
        logger.debug("Retrieved credential for %s", account)
    else:
        logger.debug("No credential found for %s", account)
    return value


def delete_credential(account: str) -> None:
    """
    Delete a credential from the macOS Keychain.

    Args:
        account: Identifier to delete.
    """
    import keyring
    try:
        keyring.delete_password(SERVICE_NAME, account)
        logger.info("Deleted credential for %s", account, extra={"event_type": "credential_deleted"})
    except keyring.errors.PasswordDeleteError:
        logger.warning("No credential to delete for %s", account)


def list_accounts() -> list[str]:
    """
    List all accounts with stored credentials.

    Note: keyring doesn't natively support listing. This checks
    known account patterns.
    """
    known_patterns = [
        "openai_api_key",
        "anthropic_api_key",
    ]
    results = []
    import keyring
    for account in known_patterns:
        if keyring.get_password(SERVICE_NAME, account):
            results.append(account)
    return results
