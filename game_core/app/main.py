""" Game core """

import os
import structlog  # type: ignore
from pydantic import ValidationError
from google.cloud.functions.context import Context # type:  ignore

import firebase_admin # type:  ignore
from firebase_admin import firestore # type:  ignore

from utils.models import NewGameData
from quest_system import get_quest_by_name, QuestLoadError
from quest_system.quests.intro import IntroQuest

FIRST_QUEST = IntroQuest.__name__

app = firebase_admin.initialize_app()
db = firestore.client()

logger = structlog.get_logger().bind(version=os.environ.get("APP_VERSION", "test"))
logger.info("Started")


def create_new_game(event: dict, context: Context):
    """ Create a new game """
    logger.info("Got create new game request", event=event)

    # decode event
    try:
        new_game_data = NewGameData.parse_event(event)
    except ValidationError as err:
        logger.error("Validation error", err=err)
        raise err

    logger.info("Resolved data", new_game_data=new_game_data)

    # create game if doesn't exist
    game_id = f"github:{new_game_data.userId}"
    game_ref = db.collection("game").document(game_id)
    game = game_ref.get()
    if game.exists:
        logger.info("Game already exists", game_id=game_id)
        game_ref.set(
            {
                **new_game_data.dict(),
            },
            merge=True,
        )
    else:
        logger.info("Creating new game", game_id=game_id)
        game_ref.set(
            {
                **new_game_data.dict(),
                "joined": firestore.SERVER_TIMESTAMP,
            }
        )

    # create starting quest if not exist
    FirstQuest = get_quest_by_name(FIRST_QUEST)
    quest_obj = FirstQuest()

    quest_id = f"{game_id}:{FirstQuest.__name__}"
    quest_ref = db.collection("quest").document(quest_id)

    quest = quest_ref.get()
    if quest.exists:
        logger.info("Quest already exists, updating", quest_id=quest_id)

        try:
            quest_obj.load(quest.to_dict())
        except QuestLoadError as err:
            logger.error("Could not load", err=err)
            raise err
    else:
        quest_obj.new()

    game_ref.set(quest_obj.get_save_data())