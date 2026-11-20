"""List-or-comma-string argument shapes for the ticket tool (STOMPY-3814).

Reads return tags and ids as lists, so agents send lists; the handlers already parse
the comma string. Both shapes are normalised to that string before the handler runs,
so stored results are identical either way. A bare number is still refused.
"""

from typing import Annotated, List, Union

from pydantic import AfterValidator


def _comma(value):
    return ",".join(str(v) for v in value) if isinstance(value, list) else value


TagsArg = Annotated[Union[str, List[str]], AfterValidator(_comma)]
IdsArg = Annotated[Union[str, List[Union[int, str]]], AfterValidator(_comma)]
