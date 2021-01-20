""" The intro quest """

from typing import TYPE_CHECKING
from semver import VersionInfo  # type:  ignore
from ..quest import Quest, Difficulty


class IntroQuest(Quest):
    version = VersionInfo.parse("0.1.0")
    difficulty = Difficulty.BEGINNER
    description = "The intro quest"


if TYPE_CHECKING:  # pragma: no cover
    IntroQuest()