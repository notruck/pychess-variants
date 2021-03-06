import asyncio
from datetime import datetime
from functools import partial
import json
import logging
from time import monotonic

from aiohttp import web

from broadcast import round_broadcast, lobby_broadcast
from const import ANALYSIS, STARTED, INVALIDMOVE, VARIANTS
from seek import get_seeks, Seek
from utils import load_game, get_board
from settings import FISHNET_KEYS

log = logging.getLogger(__name__)

MOVE_WORK_TIME_OUT = 5.0


async def get_work(request, data):
    fm = request.app["fishnet_monitor"]
    key = data["fishnet"]["apikey"]
    worker = FISHNET_KEYS[key]

    fishnet_work_queue = request.app["fishnet"]

    # priority can be "move" or "analysis"
    try:
        (priority, work_id) = fishnet_work_queue.get_nowait()
        work = request.app["works"][work_id]
        # print("FISHNET ACQUIRE we have work for you:", work)
        if priority == ANALYSIS:
            fm[worker].append("%s %s %s %s of %s moves" % (datetime.utcnow(), work_id, "request", "analysis", work["moves"].count(" ") + 1))

            # delete previous analysis
            gameId = work["game_id"]
            game = await load_game(request.app, gameId)
            if game is None:
                return web.Response(status=204)

            for step in game.steps:
                if "analysis" in step:
                    del step["analysis"]

            users = request.app["users"]
            user_ws = users[work["username"]].game_sockets[work["game_id"]]
            response = {"type": "roundchat", "user": "", "room": "spectator", "message": "Work for fishnet sent..."}
            await user_ws.send_json(response)
        else:
            fm[worker].append("%s %s %s %s for level %s" % (datetime.utcnow(), work_id, "request", "move", work["work"]["level"]))

        return web.json_response(work, status=202)
    except asyncio.QueueEmpty:
        # There was no new work in the queue. Ok
        # Now let see are there any long time pending work in app["works"]
        # (in case when worker grabbed it from queue but not responded after MOVE_WORK_TIME_OUT secs)
        pending_works = request.app["works"]
        now = monotonic()
        for work_id in pending_works:
            work = pending_works[work_id]
            if work["work"]["type"] == "move" and (now - work["time"] > MOVE_WORK_TIME_OUT):
                fm[worker].append("%s %s %s %s for level %s" % (datetime.utcnow(), work_id, "request", "move AGAIN", work["work"]["level"]))
                return web.json_response(work, status=202)
        return web.Response(status=204)
    except Exception:
        raise


async def fishnet_acquire(request):
    data = await request.json()

    fm = request.app["fishnet_monitor"]
    fv = request.app["fishnet_versions"]
    key = data["fishnet"]["apikey"]
    version = data["fishnet"]["version"]
    en = data["stockfish"]["name"]
    worker = FISHNET_KEYS[key]
    fv[worker] = "%s %s" % (version, en)

    if key not in FISHNET_KEYS:
        return web.Response(status=404)

    if key not in request.app["workers"]:
        request.app["workers"].add(key)
        fm[worker].append("%s %s %s" % (datetime.utcnow(), "-", "joined"))
        request.app["users"]["Fairy-Stockfish"].bot_online = True

        if not request.app["users"]["Fairy-Stockfish"].seeks:
            ai = request.app["users"]["Fairy-Stockfish"]
            seeks = request.app["seeks"]
            sockets = request.app["websockets"]
            for variant in VARIANTS:
                variant960 = variant.endswith("960")
                variant_name = variant[:-3] if variant960 else variant
                seek = Seek(ai, variant_name, color="r", base=5, inc=3, level=6, chess960=variant960)
                seeks[seek.id] = seek
                ai.seeks[seek.id] = seek
            await lobby_broadcast(sockets, get_seeks(seeks))

    response = await get_work(request, data)
    return response


async def fishnet_analysis(request):
    work_id = request.match_info.get("workId")
    data = await request.json()

    fm = request.app["fishnet_monitor"]
    key = data["fishnet"]["apikey"]
    worker = FISHNET_KEYS[key]

    # print(json.dumps(data, sort_keys=True, indent=4))
    if key not in FISHNET_KEYS:
        return web.Response(status=404)

    work = request.app["works"][work_id]
    fm[worker].append("%s %s %s" % (datetime.utcnow(), work_id, "analysis"))

    gameId = work["game_id"]
    game = await load_game(request.app, gameId)

    # bot_name = data["stockfish"]["name"]

    users = request.app["users"]
    username = work["username"]

    try:
        user_ws = users[username].game_sockets[gameId]
    except KeyError:
        log.error("Can't send analysis to %s. Game %s was removed from game_sockets !!!" % (username, gameId))
        return web.Response(status=204)

    length = len(data["analysis"])
    for j, analysis in enumerate(reversed(data["analysis"])):
        i = length - j - 1
        if analysis is not None:
            try:
                if "analysis" not in game.steps[i]:
                    # TODO: save PV only for inaccuracy, mistake and blunder
                    # see https://github.com/ornicar/lila/blob/master/modules/analyse/src/main/Advice.scala
                    game.steps[i]["analysis"] = {
                        "s": analysis["score"],
                        "d": analysis["depth"],
                        "p": analysis["pv_san"],
                        "m": analysis["pv"].partition(" ")[0]  # save first PV move to draw advice arrow
                    }
                else:
                    continue
            except KeyError:
                game.steps[i]["analysis"] = {
                    "s": analysis["score"],
                }

            ply = str(i)
            # response = {"type": "roundchat", "user": bot_name, "room": "spectator", "message": ply + " " + json.dumps(analysis)}
            # await user_ws.send_json(response)

            response = {"type": "analysis", "ply": ply, "color": "w" if i % 2 == 0 else "b", "ceval": game.steps[i]["analysis"]}
            await user_ws.send_json(response)

    # remove completed work
    if all(data["analysis"]):
        del request.app["works"][work_id]
        new_data = {"a": [step["analysis"] for step in game.steps]}
        await request.app["db"].game.find_one_and_update({"_id": game.id}, {"$set": new_data})

    return web.Response(status=204)


async def fishnet_move(request):
    work_id = request.match_info.get("workId")
    data = await request.json()

    fm = request.app["fishnet_monitor"]
    key = data["fishnet"]["apikey"]
    worker = FISHNET_KEYS[key]

    # print(json.dumps(data, sort_keys=True, indent=4))
    if key not in FISHNET_KEYS:
        return web.Response(status=404)

    fm[worker].append("%s %s %s" % (datetime.utcnow(), work_id, "move"))

    if work_id not in request.app["works"]:
        response = await get_work(request, data)
        return response

    work = request.app["works"][work_id]
    gameId = work["game_id"]

    # remove work from works
    del request.app["works"][work_id]

    game = await load_game(request.app, gameId)
    if game is None:
        return web.Response(status=204)

    users = request.app["users"]
    games = request.app["games"]
    username = "Fairy-Stockfish"

    move = data["move"]["bestmove"]

    invalid_move = False
    log.info("BOT move %s %s %s %s - %s" % (username, gameId, move, game.wplayer.username, game.bplayer.username))
    if game.status <= STARTED:
        try:
            await game.play_move(move)
        except SystemError:
            invalid_move = True
            log.error("Game %s aborted because invalid move %s by %s !!!" % (gameId, move, username))
            game.status = INVALIDMOVE
            game.result = "0-1" if username == game.wplayer.username else "1-0"

    opp_name = game.wplayer.username if username == game.bplayer.username else game.bplayer.username

    if not invalid_move:
        board_response = get_board(games, {"gameId": gameId}, full=False)

    if users[opp_name].bot:
        if game.status > STARTED:
            await users[opp_name].game_queues[gameId].put(game.game_end)
        else:
            await users[opp_name].game_queues[gameId].put(game.game_state)
    else:
        try:
            opp_ws = users[opp_name].game_sockets[gameId]
            if not invalid_move:
                await opp_ws.send_json(board_response)
            if game.status > STARTED:
                await opp_ws.send_json(game.game_end)
        except KeyError:
            log.error("Move %s can't send to %s. Game %s was removed from game_sockets !!!" % (move, username, gameId))

    if not invalid_move:
        await round_broadcast(game, users, board_response, channels=request.app["channels"])

    response = await get_work(request, data)
    return response


async def fishnet_abort(request):
    work_id = request.match_info.get("workId")
    data = await request.json()

    fm = request.app["fishnet_monitor"]
    key = data["fishnet"]["apikey"]
    worker = FISHNET_KEYS[key]

    if key not in FISHNET_KEYS:
        return web.Response(status=404)

    fm[worker].append("%s %s %s" % (datetime.utcnow(), work_id, "abort"))

    # remove fishnet client
    try:
        request.app["workers"].remove(data["fishnet"]["apikey"])
    except KeyError:
        log.debug("Worker %s was was already removed" % key)

    # re-schedule the job
    request.app["fishnet"].put_nowait((ANALYSIS, work_id))

    if len(request.app["workers"]) == 0:
        request.app["users"]["Fairy-Stockfish"].bot_online = False
        # TODO: msg to work user

    return web.Response(status=204)


async def fishnet_key(request):
    key = request.match_info.get("key")
    if key not in FISHNET_KEYS:
        return web.Response(status=404)

    return web.Response()


async def fishnet_monitor(request):
    fm = request.app["fishnet_monitor"]
    fv = request.app["fishnet_versions"]
    workers = {worker + " v" + fv[worker]: list(fm[worker]) for worker in fm if fm[worker]}
    return web.json_response(workers, dumps=partial(json.dumps, default=datetime.isoformat))
