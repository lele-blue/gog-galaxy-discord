import asyncio
import json
import logging as log
import os
import re
import secrets
import subprocess
import sys
import webbrowser
from threading import Thread
from typing import Union, List, Any, Tuple

import psutil
import trio
from cdp import target, page, dom, runtime, network
from cdp.dom import Node, NodeId
from cdp.network import ResponseReceived
from cdp.runtime import RemoteObject
from galaxy.api.consts import LicenseType, Platform, LocalGameState
from galaxy.api.plugin import create_and_run_plugin, Plugin
from galaxy.api.types import Game, LicenseInfo, FriendInfo, Authentication, LocalGame
from trio_cdp import open_cdp_connection, CdpConnection, CdpSession
from trio_websocket import HandshakeError

DEVTOOLS_BROWSER_LAUNCH_OUTPUT_REGEX = r"DevTools listening on ws://127\.0\.0\.1:31337/devtools/browser/(.+)"

LOG_SENSITIVE_DATA = False

RESTART_DISCORD = True

devtools_url = None

returned_from_trio_run = None


class DiscordPlugin(Plugin):
    async def get_owned_games(self) -> List[Game]:
        await self.ensure_discord_scraped()
        return self.games

    async def get_local_games(self) -> List[LocalGame]:
        await self.ensure_discord_scraped()
        local_games = []
        for game in self.games:
            local_games.append(LocalGame(game_id=game.game_id, local_game_state=LocalGameState.Installed))

        return local_games

    async def launch_game(self, game_id: str) -> None:
        webbrowser.open_new(f'discord:///library/{game_id}/launch')

    async def get_friends(self) -> List[FriendInfo]:
        await self.ensure_discord_scraped()
        return self.friends

    def __init__(self, reader, writer, token):
        super().__init__(
            Platform.Discord,  # Choose platform from available list
            "0.1",  # Version
            reader,
            writer,
            token
        )

        self.games = []
        self.friends = []
        self.user_email = ""

        if not devtools_url:
            prepare_and_discover_discord()

    async def ensure_discord_scraped(self):
        # This is not a sufficient check. Suppose that the user adds a new friend after being authenticated with the
        # Discord client. This new friend would not appear unless the client was scraped again, since self.user_email
        # is already defined. (A similar situation occurs with games.)

        # The only way to ensure that the information remains relevant is to continue to scrape the Discord client
        # afterwards. (Alternatively, if you are feeling lazy, another "solution" would be to disconnect the user from
        # the plugin after some time (such as by raising InvalidCredentials()). This is obviously not a preferable
        # solution, since having to constantly relaunch Discord is inconvenient for the user.)
        if not self.user_email:
            await self.scrape_discord()

    async def scrape_discord(self):
        global returned_from_trio_run
        t = Thread(target=run_trio_start)
        t.start()
        while 1:
            if returned_from_trio_run is not None:
                log.debug(returned_from_trio_run)
                await asyncio.sleep(1)
                log.debug(returned_from_trio_run)
                self.user_email = str(returned_from_trio_run[2])[1:-1]
                self.games = returned_from_trio_run[0]
                self.friends = returned_from_trio_run[1]
                return
            await asyncio.sleep(1)

    # implement methods
    async def authenticate(self, stored_credentials=None):
        await self.ensure_discord_scraped()
        return Authentication(self.user_email, self.user_email)


def main():
    create_and_run_plugin(DiscordPlugin, sys.argv)


def prepare_and_discover_discord():
    global devtools_url
    for proc in psutil.process_iter():
        if not proc.is_running():
            continue
        if proc.name() == "Discord.exe":
            if len(proc.cmdline()) < 3 or RESTART_DISCORD:
                if len(proc.cmdline()) == 1 or (RESTART_DISCORD and len(proc.cmdline()) == 2):
                    path = proc.exe()
                    proc.kill()
                    process = subprocess.Popen([path, "--remote-debugging-port=31337"], stderr=subprocess.PIPE)
                    while True:
                        output = process.stderr.readline()
                        if output == '' and process.poll() is not None:
                            break
                        if output:
                            line = str(output.strip(), encoding="UTF8")
                            if re.match(DEVTOOLS_BROWSER_LAUNCH_OUTPUT_REGEX, line):
                                devtools_url = re.search(DEVTOOLS_BROWSER_LAUNCH_OUTPUT_REGEX, line)[1]
                                print(devtools_url)
                                break

                    rc = process.poll()
                    continue
                if proc.cmdline()[1] == "--remote-debugging-port=31337":
                    pass


async def start(rec_tries=0):
    if rec_tries > 100:
        raise SystemExit("Max retries reached")
    global returned_from_trio_run
    # await trio.sleep(10)
    try:
        async with open_cdp_connection(
                "ws://127.0.0.1:31337/devtools/browser/" + devtools_url) as conn:  # type: CdpConnection
            targets = await conn.execute(target.get_targets())
            target_id = targets[0].id
            session = await conn.open_session(target_id)

            # Navigate to a website.
            await session.execute(page.enable())
            # async with session.wait_for(page.LoadEventFired):
            #    ...  # await session.execute(page.navigate(target_url))

            '''
            Is this even necessary? What exactly is the page title being used for?
             # Extract the page title.
            root_node = await session.execute(dom.get_document())
            title_node_id = await session.execute(dom.query_selector(root_node.node_id, 'title'))
            html = await session.execute(dom.get_outer_html(title_node_id))
            print(html)
            '''

            await trio.sleep(5)

            returned_from_trio_run = (await get_games(session), await get_friends(session), await
                                      get_user_email(session))
    except HandshakeError:
        await trio.sleep(2)
        return await start(rec_tries+1)


async def open_friends_page(session: CdpSession):
    await session.execute(runtime.evaluate("""document.querySelector('a[aria-label="Home"][href]').click()"""))
    await session.execute(runtime.evaluate("""document.querySelector("a[href='/channels/@me']").click()"""))
    await session.execute(runtime.evaluate(
        """document.querySelectorAll("svg[name='PersonWaving']")[1].parentElement.parentElement.querySelectorAll
        ("div[role='button']")[2].click()"""))


async def get_data_from_local_cache(session: CdpSession, data: str):
    nonce = secrets.token_urlsafe(20).replace("-", "_")
    # reconstruct the localStorage object that discord has hidden from me to extract games
    # code borrowed from https://stackoverflow.com/a/53773662/6508769 TYSM for the answer it saved me :P
    # modified to not modify the client and not to break any TOS
    a: Union[Tuple[RemoteObject], Any] = await session.execute(runtime.evaluate(f"""
            (function () {{
              function g_{nonce}() {{
                  const iframe = document.createElement('iframe');
                  document.body.append(iframe);
                  const pd = Object.getOwnPropertyDescriptor(iframe.contentWindow, 'localStorage');
                  iframe.remove();
                  return pd;
                }};
            return g_{nonce}().get.apply().{data}      
        }})()"""))
    return a[0].value


async def get_user_email(session: CdpSession):
    log.debug("DISCORD_SCRAPE_EMAIL: Scraping the user's e-mail from the Discord client...")
    email = await get_data_from_local_cache(session, "email_cache")
    if LOG_SENSITIVE_DATA:
        log.debug(f"DISCORD_SCRAPE_EMAIL_FINISHED: The user's e-mail address {str(email)} was found from the "
                  f"Discord client!")
    else:
        log.debug(f"DISCORD_SCRAPE_EMAIL_FINISHED: The user's e-mail address {str(email)[:1]}*** was found from "
                  f"the Discord client!")
    return email


async def get_games(session: CdpSession):
    log.debug("DISCORD_SCRAPE_GAMES: Scraping the user's games from the Discord client...")
    games = []
    games_json = await get_data_from_local_cache(session, "InstallationManagerStore")
    if not json.loads(games_json)["_state"]["installationPaths"]:
        log.debug("DISCORD_SCRAPED_GAMES: [] (The user has no games on Discord!)")
        return []

    games_string = ""
    for path in json.loads(games_json)["_state"]["installationPaths"]:
        for folder in os.path.os.listdir(path):
            if os.path.isdir(os.path.join(path, folder)):
                info_file_path = os.path.join(os.path.join(path, folder, "application_info.json"))
                if os.path.isfile(info_file_path):
                    app_info = json.loads(open(info_file_path).read())
                    games.append(Game(app_info["application_id"], app_info["name"], [],
                                      LicenseInfo(LicenseType.SinglePurchase)))
                    games_string += (str(app_info["name"]) + ", ")
    log.debug(f"DISCORD_SCRAPED_GAMES: [{games_string[-1:]}]")
    return games


async def get_friends(session: CdpSession):
    log.debug("DISCORD_SCRAPE_FRIENDS: Scraping the user's friends from the Discord client...")
    await open_friends_page(session)
    root_node: Union[Node, dict] = await session.execute(dom.get_document())
    friend_node_ids: Union[Any, List[NodeId]] = await session.execute(
        dom.query_selector_all(root_node.node_id, "div[class^='friendsRow']"))
    friends = []
    for friend_node_id in friend_node_ids:
        username_node_id: Union[NodeId, Any] = await session.execute(
            dom.query_selector(friend_node_id, "span[class^='username-']"))
        username = await session.execute(dom.get_outer_html(username_node_id))
        username = re.search(r'<span class=".+">(.+)</span>', str(username))[1]
        discriminator_node_id: Union[NodeId, Any] = await session.execute(
            dom.query_selector(friend_node_id, "span[class^='discriminator-']"))
        discriminator = await session.execute(dom.get_outer_html(discriminator_node_id))
        discriminator = re.search(r'<span class=".+">#(.+)</span>', str(discriminator))[1]
        if LOG_SENSITIVE_DATA:
            log.debug(f"DISCORD_FRIEND: Found {username} (Discriminator: {discriminator})")
        else:
            log.debug(f"DISCORD_FRIEND: Found {username[:1]}*** (Discriminator: ***)")
        friends.append(FriendInfo(f"{username}#{discriminator}", username))
    log.debug("DISCORD_SCRAPE_FRIENDS_FINISHED: The user's list of friends was successfully found from the Discord "
              "client!")
    return friends


def run_trio_start():
    trio.run(start, restrict_keyboard_interrupt_to_checkpoints=False)


# run plugin event loop
if __name__ == "__main__":
    main()
