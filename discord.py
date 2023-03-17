import datetime
import json
import logging as log
import os
import re
import secrets
import subprocess
import sys
import webbrowser
import websocket
from typing import List

import psutil
from galaxy.api.consts import LicenseType, Platform, LocalGameState
from galaxy.api.errors import InvalidCredentials
from galaxy.api.plugin import create_and_run_plugin, Plugin
from galaxy.api.types import Game, LicenseInfo, FriendInfo, Authentication, LocalGame
from galaxy.http import create_client_session

DEBUGGING_PORT = 31337

DEVTOOLS_BROWSER_LAUNCH_OUTPUT_REGEX = rf"DevTools listening on ws://127\.0\.0\.1:{DEBUGGING_PORT}/devtools/browser/" \
                                       rf"(.+)"

IS_WINDOWS = (sys.platform == "win32")

LOG_SENSITIVE_DATA = False

RESTART_DISCORD = True

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/78.0.3904.87 "
              "Safari/537.36")

devtools_url = None

returned_from_trio_run = None

ws_url = None


class DiscordPlugin(Plugin):
    async def get_owned_games(self) -> List[Game]:
        await self.ensure_discord_scraped()
        return self.games

    async def get_local_games(self) -> List[LocalGame]:
        # await self.ensure_discord_scraped()
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
            "0.1.1",  # Version
            reader,
            writer,
            token
        )

        self.games = []
        self.friends = []
        self.user_email = ""

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
        await start()
        log.debug(returned_from_trio_run)
        self.user_email = str(returned_from_trio_run[2])[1:-1]
        self.games = returned_from_trio_run[0]
        self.friends = returned_from_trio_run[1]
        kill_command = "taskkill /im Discord.exe" if IS_WINDOWS else "killall -KILL Discord"
        subprocess.Popen(kill_command)

    # implement methods
    async def authenticate(self, stored_credentials=None):
        if not stored_credentials:
            log.debug("DISCORD_RESTART: Restarting Discord...")
            await prepare_and_discover_discord()
            await get_ws_url()
            try:
                await self.scrape_discord()
            except Exception:
                log.exception("DISCORD_AUTH_FAILURE: A critical exception was thrown when scraping the Discord client.")
                raise InvalidCredentials()
            if self.user_email:
                self.store_credentials({"user_email": self.user_email})
            else:
                raise InvalidCredentials()
        else:
            self.user_email = stored_credentials["user_email"]
        return Authentication(self.user_email, self.user_email)


def main():
    create_and_run_plugin(DiscordPlugin, sys.argv)


async def get_ws_url():
    global ws_url
    async with create_client_session() as session:
        log.debug("DISCORD_WS_CHECK: Retrieving the WebSocket debugger URL...")
        headers = {
            "User-Agent": USER_AGENT
        }
        resp = await session.get(f"http://localhost:{DEBUGGING_PORT}/json/list?t="
                                 f"{str(int(datetime.datetime.now().timestamp()))}", headers=headers)
        resp_json = await resp.json()
        ws_url = resp_json[0]["webSocketDebuggerUrl"]
        log.debug(f"DISCORD_WS_FOUND: Got WebSocket debugger URL {ws_url}!")
        # begin_url = f"http://localhost:{DEBUGGING_PORT}/devtools/inspector.html?ws={ws_url[5:]}"


async def prepare_and_discover_discord():
    global devtools_url
    for proc in psutil.process_iter():
        if not proc.is_running():
            continue
        if proc.name() == ("Discord.exe" if IS_WINDOWS else "Discord.app"):  # This should provide Mac compatibility.
            if len(proc.cmdline()) < 3 or RESTART_DISCORD:
                if len(proc.cmdline()) == 1 or (RESTART_DISCORD and len(proc.cmdline()) == 2):
                    path = proc.exe()
                    proc.kill()
                    process = subprocess.Popen([path, f"--remote-debugging-port={DEBUGGING_PORT}"],
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    while True:
                        output = process.stdout.readline().decode("UTF-8")
                        log.debug("Line: " + output)
                        if output == "" and process.poll() is not None:
                            break
                        if output.startswith("DevTools listening on"):
                            devtools_url = re.search(DEVTOOLS_BROWSER_LAUNCH_OUTPUT_REGEX, output)
                            log.debug(f"DISCORD_DEVTOOLS_URL: {devtools_url}")
                            break
                    log.debug(f"DISCORD_RESTART_FINISHED: The Discord client has been successfully launched with remote"
                              f" debugging enabled on port {DEBUGGING_PORT}!")
                    return
                if proc.cmdline()[1] == f"--remote-debugging-port={DEBUGGING_PORT}":
                    return


async def start(rec_tries=0):
    if rec_tries > 100:
        log.debug("DISCORD_SCRAPE_FAILED: The maximum number of retries has been reached.")
        raise InvalidCredentials()
    global returned_from_trio_run
    # await trio.sleep(10)
    ws = websocket.WebSocket()
    ws.connect(ws_url)
    returned_from_trio_run = (await get_games(ws), await get_friends(ws), await get_user_email(ws))


async def open_friends_page(ws: websocket.WebSocket):
    # Simulate the user clicking on the "Home" button.
    msg = runtime_evaluate_json(r"document.querySelector('a[aria-label=\"Home\"][href]').click()")
    ws.send(msg)
    ws.recv()
    # Simulate the user clicking on the "Friends" button.
    msg = runtime_evaluate_json(r"document.querySelector(\"a[href='/channels/@me']\").click()")
    ws.send(msg)
    ws.recv()
    # Navigates to the PersonWaving icon, goes up two elements, and then selects the second button (All) to show all of
    # the user's friends.
    msg = runtime_evaluate_json(r"document.querySelectorAll(\"svg[name='PersonWaving']\")[1].parentElement."
                                r"parentElement.querySelectorAll(\"div[role='button']\")[2].click()")
    ws.send(msg)
    ws.recv()


def create_ws_json(method: str, params=None):
    if params is None:
        return rf"""
                {{
                    "id": 1,
                    "method": "{method}"
                }}
                """

    return rf"""
            {{
                "id": 1,
                "method": "{method}",
                "params": {params}
            }}
            """


def runtime_evaluate_json(expression: str):
    return create_ws_json("Runtime.evaluate", rf'''
    {{
        "expression": "{expression}"
    }}
    ''')


async def get_data_from_local_cache(ws: websocket.WebSocket, data: str):
    nonce = secrets.token_urlsafe(20).replace("-", "_")
    # reconstruct the localStorage object that discord has hidden from me to extract games
    # code borrowed from https://stackoverflow.com/a/53773662/6508769 TYSM for the answer it saved me :P
    # modified to not modify the client and not to break any TOS
    msg = runtime_evaluate_json(f"""
            (function () {{
              function g_{nonce}() {{
                  const iframe = document.createElement('iframe');
                  document.body.append(iframe);
                  const pd = Object.getOwnPropertyDescriptor(iframe.contentWindow, 'localStorage');
                  iframe.remove();
                  return pd;
                }};
            return g_{nonce}().get.apply().{data}      
        }})()""")
    log.debug("DISCORD_LOCAL_STORAGE_REQUEST: " + str(msg))
    ws.send(msg)
    while True:
        resp = ws.recv()
        if 'result' in json.loads(resp):
            resp_dict = json.loads(resp)
            break
    if LOG_SENSITIVE_DATA:
        log.debug(f"DISCORD_RESPONSE_FOR_{data}: {str(resp_dict)}")
    return resp_dict['result']['result']['value']


async def get_user_email(ws: websocket.WebSocket):
    log.debug("DISCORD_SCRAPE_EMAIL: Scraping the user's e-mail from the Discord client...")
    email = await get_data_from_local_cache(ws, "email_cache")
    if LOG_SENSITIVE_DATA:
        log.debug(f"DISCORD_SCRAPE_EMAIL_FINISHED: The user's e-mail address {str(email[1:-1])} was found from the "
                  f"Discord client!")
    else:
        log.debug(f"DISCORD_SCRAPE_EMAIL_FINISHED: The user's e-mail address {str(email)[1:2]}*** was found from "
                  f"the Discord client!")
    return email


async def get_games(ws: websocket.WebSocket):
    log.debug("DISCORD_SCRAPE_GAMES: Scraping the user's games from the Discord client...")
    games = []
    games_json = await get_data_from_local_cache(ws, "InstallationManagerStore")
    if not json.loads(games_json)["_state"]["installationPaths"]:
        log.debug("DISCORD_SCRAPED_GAMES: [] (The user has no games on Discord!)")
        return []

    games_string = ""
    for path in json.loads(games_json)["_state"]["installationPaths"]:
        if os.path.isdir(path):
            for folder in os.path.os.listdir(path):
                if os.path.isdir(os.path.join(path, folder)):
                    info_file_path = os.path.join(os.path.join(path, folder, "application_info.json"))
                    if os.path.isfile(info_file_path):
                        app_info = json.loads(open(info_file_path).read())
                        games.append(Game(app_info["application_id"], app_info["name"], [],
                                          LicenseInfo(LicenseType.SinglePurchase)))
                        games_string += (str(app_info["name"]) + ", ")
    log.debug(f"DISCORD_SCRAPED_GAMES: [{games_string[:-2]}]")
    return games


async def get_friends(ws: websocket.WebSocket):
    log.debug("DISCORD_SCRAPE_FRIENDS: Scraping the user's friends from the Discord client...")
    await open_friends_page(ws)
    msg = create_ws_json("DOM.getDocument")
    ws.send(msg)
    root_node = ws.recv()
    root_node_id = json.loads(root_node)['result']['root']['nodeId']
    msg = create_ws_json("DOM.querySelectorAll", rf'''
        {{
            "nodeId": {root_node_id},
            "selector": "div[class^='friendsRow']"
        }}
    ''')
    ws.send(msg)
    while True:
        resp = ws.recv()
        if 'result' in json.loads(resp):
            friend_node_ids = json.loads(resp)['result']['nodeIds']
            break
    friends = []
    for friend_node_id in friend_node_ids:
        msg = create_ws_json("DOM.querySelector", rf'''
        {{
            "nodeId": {friend_node_id},
            "selector": "span[class^='username-']"
        }}
        ''')
        ws.send(msg)
        while True:
            resp = ws.recv()
            if 'result' in json.loads(resp):
                username_node_id = json.loads(resp)['result']['nodeId']
                break
        msg = create_ws_json("DOM.getOuterHTML", rf'''
        {{
            "nodeId": {username_node_id}
        }}
        ''')
        ws.send(msg)
        while True:
            resp = ws.recv()
            if 'result' in json.loads(resp):
                username = json.loads(resp)['result']['outerHTML']
                break
        username = re.search(r'<span class=".+">(.+)</span>', str(username))[1]

        msg = create_ws_json("DOM.querySelector", rf'''
        {{
            "nodeId": {friend_node_id},
            "selector": "span[class^='discriminator-']"
         }}
         ''')
        ws.send(msg)
        while True:
            resp = ws.recv()
            if 'result' in json.loads(resp):
                discriminator_node_id = json.loads(resp)['result']['nodeId']
                break
        msg = create_ws_json("DOM.getOuterHTML", rf'''
                {{
                    "nodeId": {discriminator_node_id}
                }}
                ''')
        ws.send(msg)
        while True:
            resp = ws.recv()
            if 'result' in json.loads(resp):
                discriminator = json.loads(resp)['result']['outerHTML']
                break
        discriminator = re.search(r'<span class=".+">#(.+)</span>', str(discriminator))[1]
        if LOG_SENSITIVE_DATA:
            log.debug(f"DISCORD_FRIEND: Found {username} (Discriminator: {discriminator})")
        else:
            log.debug(f"DISCORD_FRIEND: Found {username[:1]}*** (Discriminator: ***)")
        friends.append(FriendInfo(f"{username}#{discriminator}", username))
    log.debug("DISCORD_SCRAPE_FRIENDS_FINISHED: The user's list of friends was successfully found from the Discord "
              "client!")
    return friends


# run plugin event loop
if __name__ == "__main__":
    main()
