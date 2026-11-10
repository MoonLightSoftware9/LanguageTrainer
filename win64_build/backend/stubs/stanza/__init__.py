"""Lightweight stand-in for the real ``stanza`` package.

argostranslate hard-imports ``stanza`` in ``argostranslate/sbd.py`` (an unguarded
``import stanza``) even though this application selects the spaCy sentencizer via
``ARGOS_CHUNK_TYPE=SPACY`` and never constructs a ``StanzaSentencizer``. The real
``stanza`` pulls in ``torch`` (~1 GB, plus CUDA/triton on Linux) purely to satisfy
that import — it is never exercised at runtime here.

Shipping this stub lets argostranslate import without ``stanza``/``torch``, with
byte-identical translation output (verified: the spaCy sentencizer path is used
regardless). If the stanza code path is ever actually reached, these raise a clear
error instead of silently misbehaving.
"""

__version__ = "0.0.0+stub"


def _stanza_unavailable(*_args, **_kwargs):
    raise RuntimeError(
        "This build ships a stub 'stanza' package (torch-free). Sentence "
        "segmentation uses spaCy (ARGOS_CHUNK_TYPE=SPACY); StanzaSentencizer is "
        "not available. Install the real 'stanza' package to use it."
    )


class Pipeline:  # argostranslate references stanza.Pipeline only inside StanzaSentencizer
    def __init__(self, *args, **kwargs):
        _stanza_unavailable()


def download(*args, **kwargs):
    _stanza_unavailable()
