""" Base Classes for quest objects """
from __future__ import annotations

from typing import List, Dict, ClassVar, Type, Generator, cast, TYPE_CHECKING

from abc import ABC, abstractmethod
from inspect import isclass
from graphlib import TopologicalSorter, CycleError

from structlog import get_logger
from pydantic import ValidationError
from semver import VersionInfo  # type:  ignore

from app.firebase_utils import db
from app.tick import TickType
from .exceptions import QuestError, QuestLoadError, QuestDefinitionError
from .models import Difficulty, StorageModel, QuestBaseModel


if TYPE_CHECKING:
    from .stage import Stage  # pragma: no cover
    from app.game import Game  # pragma: no cover

logger = get_logger(__name__)


def semver_safe(start: VersionInfo, dest: VersionInfo) -> bool:
    """ whether semver loading is going to be safe """
    if start.major != dest.major:
        return False

    # check it's not a downgrade of minor version
    if start.minor > dest.minor:
        return False

    return True


class Quest(ABC):
    @classmethod
    def get_by_name(cls, name: str) -> Type[Quest]:
        from .loader import all_quests  # avoid cyclic import

        try:
            return all_quests[name]
        except KeyError as err:
            raise QuestError(f"No quest name {name}") from err

    @classmethod
    def get_first_quest(cls) -> Type[Quest]:
        from .loader import FIRST_QUEST_KEY  # avoid cyclic import

        return cls.get_by_name(FIRST_QUEST_KEY)

    @classmethod
    def from_game(cls, game: Game) -> Quest:
        key = cls.make_key(game)
        return cls(key)

    @classmethod
    def iterate_all(cls) -> Generator[Quest, None, None]:
        docs = db.collection("quest").where("complete", "!=", True).stream()
        for doc in docs:
            key = doc.id
            quest_name = cls.key_to_quest_name(key)
            QuestClass = cls.get_by_name(quest_name)
            quest = QuestClass(key)
            yield quest

    @classmethod
    def make_key(cls, game: Game) -> str:
        """ Key for referencing in database """
        return f"{game.key}:{cls.__name__}"

    @staticmethod
    def key_to_quest_name(key: str) -> str:
        return key.split(":")[-1]

    @property
    @abstractmethod
    def version(cls) -> VersionInfo:
        """ The version of this quest, used to check against load data """
        return NotImplemented

    @property
    @abstractmethod
    def difficulty(cls) -> Difficulty:
        """ Difficulty metadata, for display purposes only """
        return NotImplemented

    @property
    @abstractmethod
    def description(cls) -> str:
        """ Quest description metadata, for display purposes only """
        return NotImplemented

    @property
    @abstractmethod
    def stages(cls) -> Dict[str, Type[Stage]]:
        """ The initial default data to start quests with """
        return NotImplemented

    # default, overridable model is empty pydantic model
    QuestDataModel: ClassVar[Type[QuestBaseModel]] = QuestBaseModel

    def __init_subclass__(cls):
        """ Subclasses instantiate by copying default data """
        from .stage import Stage  # avoid cyclic import

        # build class list
        cls.stages = {}
        for name, class_var in vars(cls).items():
            if isclass(class_var) and issubclass(class_var, Stage):
                cls.stages[name] = class_var

    # loaded player quest data
    quest_data: QuestBaseModel
    completed_stages: List[str]
    graph: TopologicalSorter

    # the quest ey in the db
    key: str

    # whether complete
    complete: bool

    def __init__(self, key: str):
        if not key:
            raise ValueError("Key can't be blank")

        self.key = key
        self.load()

    def exists(self) -> bool:
        """ Whether quest exists in the database """
        quest_doc = db.collection("quest").document(self.key).get()
        return quest_doc.exists

    def load(self) -> None:
        """ Load data from storage """
        quest_doc = db.collection("quest").document(self.key).get()

        if quest_doc.exists:
            try:
                storage_model = StorageModel.parse_obj(quest_doc.to_dict())
            except ValidationError as err:
                raise QuestLoadError(
                    f"{self} Storage model validation error! {err}"
                ) from err

            self.load_storage_model(storage_model)
        else:
            self.quest_data = self.QuestDataModel()
            self.completed_stages = []
            self.complete = False

        self.load_stages()

    def save(self) -> None:
        """ Save data to storage """

        quest_ref = db.collection("quest").document(self.key)
        quest_ref.set(self.get_storage_model().dict())

    def load_storage_model(self, storage_model: StorageModel) -> None:
        """ Load save data back into structure """

        # check save version is safe before upgrading
        save_semver = VersionInfo.parse(storage_model.version)
        if not semver_safe(save_semver, self.version):
            raise QuestLoadError(
                f"{self} Unsafe version mismatch in! {save_semver} -> {self.version}"
            )

        try:
            self.quest_data = self.QuestDataModel.parse_raw(
                storage_model.serialized_data
            )
        except ValidationError as err:
            raise QuestLoadError(f"{self} data validation error! {err}") from err

        self.completed_stages = storage_model.completed_stages
        self.complete = storage_model.complete

    def get_storage_model(self) -> StorageModel:
        """ Updates save data with new version and output """
        return StorageModel(
            version=str(self.version),
            completed_stages=self.completed_stages,
            serialized_data=self.quest_data.json(),
            complete=self.complete,
        )

    def load_stages(self) -> None:
        """ loads the stages """

        # load graph
        self.graph = TopologicalSorter()
        for stage_name, StageClass in self.stages.items():
            for child_name in cast(List[str], StageClass.children):
                if child_name not in self.stages:
                    raise QuestDefinitionError(
                        f"{self} does not have stage named '{child_name}'"
                    )
                self.graph.add(child_name, stage_name)

        try:
            self.graph.prepare()
        except CycleError as err:
            raise QuestDefinitionError(f"{self} prepare failed! {err}") from err

    def execute_stages(self, tick_type: TickType) -> None:
        """ Executes stages, tick_type helps nodes know whether to skip certain stages """

        log = logger.bind(quest=self)
        log.info("Begin execution")

        while self.graph.is_active():
            ready_nodes = self.graph.get_ready()

            if not ready_nodes:
                log.info("No more ready nodes, stopping execution")
                break

            log.info("Got Ready nodes", ready_nodes=ready_nodes)

            for node in ready_nodes:
                # skip if completed, avoids triggering two final stages
                if self.complete:
                    log.info("Done flag set, skipping the rest")
                    return

                # completed node: TODO: just not put completed nodes into the graph?
                if node in self.completed_stages:
                    self.graph.done(node)
                    log.info(
                        "Node is already complete, skipping",
                        node=node,
                        complete=self.completed_stages,
                    )
                    continue

                log_node = log.bind(node=node)
                log_node.info("Begin processing stage")

                # instantiate stage and execute
                StageClass = self.stages[node]
                stage = StageClass(self)
                stage.prepare()

                if stage.condition():
                    log_node.info("Condition check passed, executing")
                    stage.execute()

                    if stage.is_done():
                        log_node.info("Stage reports done")
                        self.completed_stages.append(node)
                        self.graph.done(node)

        log.info("Done processing node")

    def __repr__(self):
        return f"{self.__class__.__name__}(key={self.key})"
