"""Exceptions of pyheltycloud."""

from __future__ import annotations


class HeltyCloudError(Exception):
    """Generic Helty cloud error."""


class HeltyCloudAuthError(HeltyCloudError):
    """Credentials rejected, or session no longer renewable."""


class HeltyCloudConnectionError(HeltyCloudError):
    """The cloud is unreachable or misbehaved."""


class HeltyCloudNoDataError(HeltyCloudError):
    """The panel has not sent telemetry recently.

    The cloud keeps no state: it only answers with the last message it
    received from the board. If the board has been silent for a while there
    is nothing to read until it is asked to report (see HeltyCloud.refresh).
    """
