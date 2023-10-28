"""
Copyright 2022 NUCOSen運営会議

This file is part of NUCOSen Broadcast.

NUCOSen Broadcast is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

NUCOSen Broadcast is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with NUCOSen Broadcast.  If not, see <https://www.gnu.org/licenses/>.
"""

import sys
from datetime import datetime, timedelta, timezone
from logging import getLogger
from os import getcwd
from traceback import format_exc

from decouple import AutoConfig

from nucosen import clock, db, live, personality, quote, sessionCookie


def run():
    logger = getLogger(__name__)

    try:
        database = db.RestDbIo()
        configLoader = AutoConfig(getcwd())

        def config(key): return str(configLoader(key, default=""))
        logininfo = config("NICO_ID"), config("NICO_PW"), config("NICO_TFA")
        if "" in logininfo:
            getLogger(__name__).info("現在のログイン情報: {0}".format(str(logininfo)))
            raise Exception("V00 ログイン情報が不十分です。現在の情報はinfoに出力済み。")

        SPECIFIC_VIDEO_IDS = [
            (config("MAINTENANCE_VIDEO_ID") or "sm17759202"),
            (config("CLOSING_VIDEO_ID") or "sm17572946")
        ]
        MAINTENANCE, CLOSING = 0, 1

        session = sessionCookie.Session(*logininfo)
        session.login()
        logger.debug("チャンネルループ開始")

        ngTags = set(config("NG_TAGS").split(","))

        while True:
            logger.debug("現枠・次枠の確保開始")
            liveIDs = live.getLives(session)
            if liveIDs[0] is None:
                if liveIDs[1] is None:
                    logger.warning("W0L 枠未検出")
                    live.reserveLive(
                        category=config("CATEGORY"),
                        communityId=config("COMMUNITY"),
                        tags=config("TAGS").split(","),
                        session=session
                    )
                    liveIDs = live.getLives(session)
                nextLive: str | None = liveIDs[0] or liveIDs[1]
                if nextLive is None:
                    raise Exception("V10 予約確認エラー")
                nextLiveBegin = live.getStartTime(nextLive, session)
                clock.waitUntil(nextLiveBegin)
                liveIDs = live.getLives(session)
            elif liveIDs[1] is None:
                live.reserveLive(
                    category=config("CATEGORY"),
                    communityId=config("COMMUNITY"),
                    tags=config("TAGS").split(","),
                    session=session
                )
            liveIDs = live.sGetLives(session)
            logger.info("現枠: {0}, 次枠: {1}".format(liveIDs[0], liveIDs[1]))

            logger.debug("現存する引用状態の処理")
            currentLiveEnd = live.getEndTime(liveIDs[0], session)
            currentQuote = quote.getCurrent(liveIDs[0], session)
            if currentQuote is not None:
                if currentQuote == SPECIFIC_VIDEO_IDS[MAINTENANCE]:
                    logger.info("メンテナンス動画の引用を検知しました")
                    quote.stop(liveIDs[0], session)
                    quote.once(
                        liveIDs[0], SPECIFIC_VIDEO_IDS[MAINTENANCE], session)
                elif currentQuote == SPECIFIC_VIDEO_IDS[CLOSING]:
                    logger.info("エンディング動画の引用を検知しました")
                    nextLiveBegin = live.getStartTime(liveIDs[1], session)
                    clock.waitUntil(currentLiveEnd)
                    live.reserveLive(
                        category=config("CATEGORY"),
                        communityId=config("COMMUNITY"),
                        tags=config("TAGS").split(","),
                        session=session
                    )
                    clock.waitUntil(nextLiveBegin)
                    liveIDs = live.sGetLives(session)
                else:
                    logger.info("一般動画の引用を検知しました: {0}".format(currentQuote))
                    quote.stop(liveIDs[0], session)
                    maintenanceSpan = quote.once(
                        liveIDs[0], SPECIFIC_VIDEO_IDS[MAINTENANCE], session)
                    maintenanceEnd = datetime.now(
                        timezone.utc) + maintenanceSpan
                    logger.error("E30 引用停止 {0}".format(currentQuote))
                    live.showMessage(
                        liveIDs[0], "システムが異常停止したため、自動回復機能により復旧しました。\n" +
                        "ご迷惑をおかけし大変申し訳ございません。まもなく再開いたします。", session)
                    clock.waitUntil(maintenanceEnd)

            currentLiveId = live.sGetLives(session)[0]
            logger.info("放送の準備が整いました: {0}".format(currentLiveId))
            while True:

                nextVideoId = database.dequeue()
                if nextVideoId is None:
                    logger.debug("キューが空なので補充を行います")
                    requests = database.getAndResetRequests()
                    if requests is not None:
                        winners = personality.choiceFromRequests(requests, 5)
                        if winners is None:
                            logger.error("E40 抽選アボート {0}".format(requests))
                            selection = personality.randomSelection(
                                config("REQTAGS").split(","), session, ngTags)
                        else:
                            selection = winners.pop()
                            database.enqueueByList(winners)
                    else:
                        selection = personality.randomSelection(
                            config("REQTAGS").split(","), session, ngTags)
                    nextVideoId = selection

                logger.info("引用を開始します: {0}".format(nextVideoId))
                currentLiveEnd = live.getEndTime(currentLiveId, session)
                videoInfo = quote.getVideoInfo(nextVideoId, session, ngTags)
                if videoInfo[0] is False:
                    raise Exception("V20 引用不能エラー {0} {1}".format(
                        nextVideoId, currentLiveId))
                if datetime.now(timezone.utc) + videoInfo[1] > currentLiveEnd - timedelta(minutes=1):
                    logger.info("引用アボート: 時間内に引用が終了しない見込みです")
                    database.priorityEnqueue(nextVideoId)
                    quote.loop(currentLiveId, config(
                        "ENDING_MOVIE_ID"), session)
                    live.showMessage(
                        currentLiveId, "この枠の放送は終了しました。\nご視聴ありがとうございました。",
                        session, permanent=True)
                    clock.waitUntil(currentLiveEnd)
                    break
                quote.once(currentLiveId, nextVideoId, session)
                live.showMessage(currentLiveId, videoInfo[2], session)
                clock.waitUntil(datetime.now(timezone.utc) + videoInfo[1])
                logger.info("引用終了見込み時刻になりました")
            logger.info("放送が終了しました: {0}".format(currentLiveId))
    except Exception:
        t = format_exc()
        logger.critical("例外がキャッチされませんでした\n```\n{0}\n```".format(t))
        sys.exit(0)
