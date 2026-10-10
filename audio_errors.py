class SynthesisError(Exception):
    """Local speech generation or alignment failed."""


class ConversionCancelled(SynthesisError):
    """The user stopped generation; completed segments remain saved."""
