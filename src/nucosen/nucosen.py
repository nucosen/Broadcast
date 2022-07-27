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
        def config(key): return str(configLoader(key))
        session = sessionCookie.Session(
            config("NICO_ID"), config("NICO_PW"), config("NICO_TFA"))
        logger.debug("チャンネルループ開始")

        while True:
            logger.debug("現枠・次枠の確保開始")
            liveIDs = live.getLives(session)
            if liveIDs[0] is None:
                if liveIDs[1] is None:
                    logger.warning("オンエア枠も次枠も見つかりませんでした。")
                    live.reserveLive(
                        category=config("CATEGORY"),
                        communityId=config("COMMUNITY"),
                        tags=config("TAGS").split(","),
                        session=session
                    )
                    liveIDs = live.getLives(session)
                nextLive: str | None = liveIDs[0] or liveIDs[1]
                if nextLive is None:
                    logger.critical("予約したはずの枠が確認できませんでした")
                    raise Exception("新しい予約の認識に失敗")
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
                if currentQuote == "sm17759202":
                    logger.info("テレビちゃん休憩中動画の引用を検知しました")
                    quote.stop(liveIDs[0], session)
                    quote.once(liveIDs[0], "sm17759202", session)
                elif currentQuote == "sm17572946":
                    logger.info("ホタルの光動画の引用を検知しました")
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
                        liveIDs[0], "sm17759202", session)
                    maintenanceEnd = datetime.now(
                        timezone.utc) + maintenanceSpan
                    logger.error("リセット処置のため{0}の引用を中断しました\n1. 直ちに放送を再開する場合はrestart\n2. この動画を再放送する場合は優先エンキュー後にrestart\n3. 調整作業は3分以内に実施".format(currentQuote))
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
                            logger.error(
                                "抽選処理を中断しました"
                                "リクエストはありましたが有効な曲がありませんでした。\n" +
                                "1. APIのフィルターを点検" +
                                "{0}".format(requests))
                            selection = personality.randomSelection(
                                config("TAGS").split(","), session)
                        else:
                            selection = winners.pop()
                            database.enqueueByList(winners)
                    else:
                        selection = personality.randomSelection(
                            config("REQTAGS").split(","), session)
                    nextVideoId = selection

                logger.info("引用を開始します: {0}".format(nextVideoId))
                currentLiveEnd = live.getEndTime(currentLiveId, session)
                videoInfo = quote.getVideoInfo(nextVideoId, session)
                if videoInfo[0] is False:
                    logger.critical(
                        "キューに引用不能な動画が含まれていました:{0}".format(nextVideoId))
                    raise Exception(
                        "引用不能な動画を引用しようとした:{0} at {1}".format(nextVideoId, currentLiveId))
                if datetime.now(timezone.utc) + videoInfo[1] > currentLiveEnd - timedelta(minutes=1):
                    logger.info("引用アボート: 時間内に引用が終了しない見込みです")
                    database.priorityEnqueue(nextVideoId)
                    quote.loop(currentLiveId, "sm17572946", session)
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
