""" AsyncChatGPT class
"""
import atexit
import base64
import json
import time
import uuid
import shutil
import asyncio
import datetime
import logging
from typing import Optional
from playwright.async_api import async_playwright
from playwright._impl._api_structures import ProxySettings
from . import error

logging.basicConfig(format='[%(asctime)s] %(message)s', datefmt='%I:%M:%S %p')
logger=logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class AsyncChatGPT:
    """
    A ChatGPT interface that uses Playwright to run a browser,
    and interacts with that browser to communicate with ChatGPT in
    order to provide an open API to ChatGPT.
    """

    stream_div_id = "chatgpt-wrapper-conversation-stream-data"
    eof_div_id = "chatgpt-wrapper-conversation-stream-data-eof"
    session_div_id = "chatgpt-wrapper-session-data"
    error_div_id = "chatgpt-wrapper-error-data"
    lock=asyncio.Lock()
    browser=None
    page=None
    enabled=True
    session={}

    def __init__(self):
        """
        Set some attributes, so that linter doesn't complain about setting attributes outside __init__.
        Should not be called. Use the async create instead.
        For sync methods, use the ChatGPT class.

        Returns:
            None
        """
        self.play = None
        self.parent_message_id=None
        self.conversation_id=None
        self.timeout=None
        self.user_data_dir=None

    @classmethod
    async def create(cls, headless: bool = True, browser="firefox", timeout=60, proxy: Optional[ProxySettings] = None, instance = None):
        """Factory method to create an AsyncChatGPT asynchronously

        Args:
            headless (bool, optional): Start headless (no GUI) browser. Defaults to True.
            browser (str, optional): Which browser to use. Defaults to "firefox".
            timeout (int, optional): Timeout waiting for ChatGPT to generate message. Defaults to 60.
            proxy (Optional[ProxySettings], optional): Network proxy. Will be passwd to playwright. Defaults to None.
            instance (object, optional): If you already have an instance, pass it here. Used in the synchronous API. When set to None, create a new one. Defaults to None.

        Returns:
            AsyncChatGPT: The AsyncChatGPT object
        """
        self=cls() if instance is None else instance
        await self.init(headless=headless,browser=browser,timeout=timeout,proxy=proxy)
        return self
    async def init(self, headless: bool = True, browser="firefox", timeout=60, proxy: Optional[ProxySettings] = None):
        """Don't use this. Use ChatGPT.create()

        This exists so that linter wouldn't complain about accessing protected members.
        """
        self.play = await async_playwright().start()

        try:
            playbrowser = getattr(self.play, browser)
        except Exception:
            logger.error(f"Browser {browser} is invalid, falling back on firefox")
            playbrowser = self.play.firefox

        if AsyncChatGPT.browser==None:
            try:
                AsyncChatGPT.browser = await playbrowser.launch_persistent_context(
                    user_data_dir="/tmp/playwright",
                    headless=headless,
                    proxy=proxy,
                )
            except Exception:
                self.user_data_dir = f"/tmp/{str(uuid.uuid4())}"
                logger.warning(f"Failed to launch browser at /tmp/playwright. Launching at {self.user_data_dir}")
                shutil.copytree("/tmp/playwright", self.user_data_dir)
                self.browser = await playbrowser.launch_persistent_context(
                    user_data_dir=self.user_data_dir,
                    headless=headless,
                    proxy=proxy,
                )
        if AsyncChatGPT.page==None:
            AsyncChatGPT.page=await AsyncChatGPT.browser.new_page()
            await AsyncChatGPT._start_browser()

        js_logger=logging.getLogger("js console")
        js_logger.setLevel(logging.INFO)
        def js_log(msg):
            if '[JavaScript Error:' in msg.text: return
            if '[JavaScript Warning:' in msg.text: return
            if msg.type in ['log','info']: js_logger.info(msg.text)
            elif msg.type=='debug': js_logger.debug(msg.text)
            elif msg.type=='warning': js_logger.warning(msg.text)
            elif msg.type=='error': js_logger.error(msg.text)
            else: js_logger.info(msg.text)
        self.page.on("console", js_log)
        self.parent_message_id = str(uuid.uuid4())
        self.conversation_id = None
        self.timeout = timeout
        atexit.register(self._cleanup)

        return self

    @classmethod
    async def _start_browser(cls):
        logger.info("Loading ChatGPT webpage...")
        await cls.page.goto("https://chat.openai.com/")
        try:
            await cls.page.wait_for_url("/chat")
        except Exception:
            errors=await cls.page.query_selector_all(f"div.cf-error-details")
            if len(errors)>0 and 'Access denied' in await errors[0].inner_html():
                logger.error("Got Access denied. Self-disabling for 10 minutes. ")
                cls.enabled=False
                await asyncio.sleep(10*60)
                cls.enabled=True
                logger.info("10 minutes passed. Re-enabling...")
                return AsyncChatGPT._start_browser()

        logger.info("ChatGPT webpage loaded")

    async def _cleanup(self):
        await self.browser.close()
        # remove the user data dir in case this is a second instance
        if hasattr(self, "user_data_dir"):
            shutil.rmtree(self.user_data_dir)
        await self.play.stop()
    async def async_refresh_session(self,timeout=15):
        """Refresh session, by redirecting the *page* to /api/auth/session rather than a simple xhr request.

        In this way, we can pass the browser check.

        Args:
            timeout (int, optional): Timeout waiting for the refresh in seconds. Defaults to 10.
        """
        logger.info("Refreshing session...")
        await AsyncChatGPT.page.goto("https://chat.openai.com/api/auth/session")
        try:
            await AsyncChatGPT.page.wait_for_url("/api/auth/session",timeout=timeout*1000)
        except Exception:
            logger.error("Timed out refreshing session. Page is now at %s. Calling _start_browser()...")
            AsyncChatGPT._start_browser()
        try:
            contents=await AsyncChatGPT.page.content()
            if "Please stand by, while we are checking your browser..." in contents:
                await asyncio.sleep(10)
                contents=await AsyncChatGPT.page.content()
            start=contents.find("{")
            if start<0:
                raise json.JSONDecodeError("A { was not found. ",contents,0)
            for index in range(len(contents)-1,0-1,-1):
                if contents[index]=='}':
                    end=index
                    break
            else:
                logger.error("A } was not found. ")
                raise json.JSONDecodeError("A } was not found. ",contents,0)
            contents=contents[start:end+1]
            logger.debug("Refreshing session received: %s",contents)
            AsyncChatGPT.session=json.loads(contents)
            logger.info("Succeessfully refreshed session. ")
        except json.JSONDecodeError:
            logger.error("Failed to decode session key. Maybe Access denied? ")
        await AsyncChatGPT._start_browser()

    async def _cleanup_divs(self):
        try:
            await self.page.evaluate(f"document.getElementById('{self.stream_div_id}').remove()")
            await self.page.evaluate(f"document.getElementById('{self.eof_div_id}').remove()")
        except Exception as err:
            logger.error("Failed to clean up divs: \n%s", err)

    async def async_ask_stream(self, prompt: str, remaining_retry=2):
        """Ask ChatGPT something by sending XHR requests. Response is streamed

        Example:
        ```python3
        async for chunk in gpt.ask_stream("Good night!"):
            # chunk will be, for example, one or a few words
            print(chunk,end='',flush=True)
        ```

        Args:
            prompt (str): Prompt to give to ChatGPT
            remaining_retry (int): How many retries remaining

        Raises:
            error.NotLoggedInError: Session is not useable. You need to log in.

        Yields:
            str: Chunks of response
        """
        logger.debug("Acquiring lock...")
        # TODO: This might not be neccessary with ChatGPT Plus ?
        async with AsyncChatGPT.lock:
            logger.debug("Got lock")
            if remaining_retry==0:
                logger.error("Cannot communicate with ChatGPT. ")
                raise error.NetworkError("Cannot communicate with ChatGPT.")
            if self.session == {}:
                await self.async_refresh_session()

            new_message_id = str(uuid.uuid4())

            if "accessToken" not in self.session:
                logger.error("Session not usable. You need to log in. ")
                raise error.NotLoggedInError(
                    "Your ChatGPT session is not usable.\n"
                    "* Run this program with the `install` parameter and log in to ChatGPT.\n"
                    "* If you think you are already logged in, try running the `session` command."
                )

            request = {
                "messages": [
                    {
                        "id": new_message_id,
                        "role": "user",
                        "content": {"content_type": "text", "parts": [prompt]},
                    }
                ],
                # TODO: why is this model specified? I've seen different ones when using ChatGPT.
                "model": "text-davinci-002-render-sha",
                "conversation_id": self.conversation_id,
                "parent_message_id": self.parent_message_id,
                "action": "next",
            }

            code = (
                """
                const stream_div = document.createElement('DIV');
                stream_div.id = "STREAM_DIV_ID";
                document.body.appendChild(stream_div);
                const xhr = new XMLHttpRequest();
                xhr.open('POST', 'https://chat.openai.com/backend-api/conversation');
                xhr.setRequestHeader('Accept', 'text/event-stream');
                xhr.setRequestHeader('Content-Type', 'application/json');
                xhr.setRequestHeader('Authorization', 'Bearer BEARER_TOKEN');
                xhr.responseType = 'stream';
                xhr.onreadystatechange = function() {
                var newEvent;
                if(xhr.readyState == 3 || xhr.readyState == 4) {
                    const newData = xhr.response.substr(xhr.seenBytes);
                    try {
                    const newEvents = newData.split(/\\n\\n/).reverse();
                    newEvents.shift();
                    if(newEvents[0] == "data: [DONE]") {
                        newEvents.shift();
                    }
                    if(newEvents.length > 0) {
                        newEvent = newEvents[0].substring(6);
                        // using XHR for eventstream sucks and occasionally ive seen incomplete
                        // json objects come through  JSON.parse will throw if that happens, and
                        // that should just skip until we get a full response.
                        JSON.parse(newEvent);
                    }
                    } catch (err) {
                    console.error(err);
                    newEvent = undefined;
                    }
                    if(newEvent !== undefined) {
                    stream_div.innerHTML = btoa(newEvent);
                    xhr.seenBytes = xhr.responseText.length;
                    }
                }
                if(xhr.readyState == 4) {
                    const eof_div = document.createElement('DIV');
                    eof_div.id = "EOF_DIV_ID";
                    document.body.appendChild(eof_div);
                }
                };
                xhr.send(JSON.stringify(REQUEST_JSON));
                """
                .replace("BEARER_TOKEN", self.session["accessToken"])
                .replace("REQUEST_JSON", json.dumps(request))
                .replace("STREAM_DIV_ID", self.stream_div_id)
                .replace("EOF_DIV_ID", self.eof_div_id)
            )
            try:
                await self.page.evaluate(code)
            except Exception as err:
                logger.warning("Error occurred when asking ChatGPT (Retrying...): %s",err)
                await self.async_refresh_session()
                async for i in self.async_ask_stream(prompt):
                    yield i
                return

            last_event_msg = ""
            start_time = time.time()
            full_event_message = None
            while time.time() - start_time <= self.timeout or full_event_message != None:
                eof_datas = await self.page.query_selector_all(f"div#{self.eof_div_id}")

                conversation_datas = await self.page.query_selector_all(
                    f"div#{self.stream_div_id}"
                )
                if len(conversation_datas) == 0:
                    continue

                full_event_message = None

                try:
                    event_raw = base64.b64decode(await conversation_datas[0].inner_html())
                    if len(event_raw) > 0:
                        event = json.loads(event_raw)
                        if event is not None:
                            self.parent_message_id = event["message"]["id"]
                            self.conversation_id = event["conversation_id"]
                            full_event_message = "\n".join(
                                event["message"]["content"]["parts"]
                            )
                except Exception as err:
                    logging.error("Failed to read response from ChatGPT: %s",err)
                    raise error.ChatGPTResponseError(
                        "Failed to read response from ChatGPT.  Tips:\n"
                        " * Try again.  ChatGPT can be flaky.\n"
                        " * Use the `session` command to refresh your session, and then try again.\n"
                        " * Restart the program in the `install` mode and make sure you are logged in."
                    )

                if full_event_message is not None:
                    chunk = full_event_message[len(last_event_msg):]
                    last_event_msg = full_event_message
                    logger.info("< (ChatGPT) %s",chunk)
                    yield chunk

                # if we saw the eof signal, this was the last event we
                # should process and we are done
                if len(eof_datas)>0:
                    break

                await asyncio.sleep(0.2)
            else:
                # Timeout
                logger.error("Timeout when asking ChatGPT. Retrying...")
                await self.async_refresh_session()
                async for chunk in self.async_ask_stream(prompt=prompt,remaining_retry=remaining_retry-1):
                    yield chunk
                return

            await self._cleanup_divs()

    async def async_ask_stream_clicking(self, prompt: str, remaining_retry=2):
        """Ask ChatGPT by clicking the buttons on the website. Response is streamed.

        Example:
        ```python3
        async for chunk in gpt.ask_stream("Good night!"):
            # chunk will be, for example, one or a few words
            print(chunk,end='',flush=True)
        ```

        Args:
            prompt (str): Prompt to give to ChatGPT
            remaining_retry (int): How many retries remaining

        Raises:
            error.NotLoggedInError: Session is not useable. You need to log in.

        Yields:
            str: Chunks of response
        """
        if remaining_retry==0:
            logger.error("Cannot ask ChatGPT. ")
            raise error.NetworkError("Cannot ask ChatGPT. ")
        if self.session is None:
            await self.async_refresh_session()

        if "accessToken" not in self.session:
            logger.error("Session not usable. You need to log in. ")
            raise error.NotLoggedInError(
                "Your ChatGPT session is not usable.\n"
                "* Run this program with the `install` parameter and log in to ChatGPT.\n"
                "* If you think you are already logged in, try running the `session` command."
            )

        prompt=prompt.replace('\n','\\n')

        code = (
            f"""
            let bottom=document.getElementsByTagName("textarea")[0].parentElement
            let textbox=bottom.children[0]
            let ask_btn=bottom.children[1]

            textbox.value="{prompt}"


            ask_btn.click()
            setTimeout(function(){{
                let answers=document.getElementsByClassName("bg-gray-50")
                let answer=answers[answers.length-1]

                let response_store = document.createElement('div')
                response_store.id = "{self.stream_div_id}"
                response_store.innerHTML = answer.innerText
                document.body.appendChild(response_store)

                let check_done=function(){{
                    let outer_judges=answer.getElementsByClassName("justify-between")[0]
                    let inner_judges=outer_judges.getElementsByTagName("div")[0]
                    if(inner_judges.classList.contains("visible")){{
                        // Done
                        let done_sign=document.createElement('div')
                        response_store.innerHTML = answer.innerText
                        done_sign.id="{self.eof_div_id}"
                        document.body.appendChild(done_sign)
                    }}else{{
                        // Not done yet
                        response_store.innerHTML = answer.innerText
                        setTimeout(check_done,200)
                    }}
                }}

                check_done()
            }},300)
            """
        )

        try:
            await self.page.evaluate(code)
        except Exception as err:
            logger.warning("Error occurred when asking ChatGPT (Retrying...): %s",err)
            await self.async_refresh_session()
            async for i in self.async_ask_stream_clicking(prompt):
                yield i
            return

        await asyncio.sleep(0.3) # Wait for response container to be created

        last_event_msg = ""
        start_time = time.time()
        response=""
        while time.time() - start_time <= self.timeout or response!="":
            eof_datas = await self.page.query_selector_all(f"div#{self.eof_div_id}")

            response_container = await self.page.query_selector_all(
                f"div#{self.stream_div_id}"
            )
            response=await response_container[0].inner_text()
            response=response.replace('\u200b','')

            if len(response) > 0:
                chunk = response[len(last_event_msg):]
                last_event_msg = response
                print(chunk,end='',flush=True)

                if 'An error occurred. If this issue persists please contact us through' in response or\
                    'The server had an error while processing your request.' in response:
                    print("Error detected when receiving response")
                    logger.error("ChatGPT gave an Error (Retrying...): %s",response)
                    # TODO: Choose the right conversation
                    await self.page.reload()
                    await asyncio.sleep(10)
                    async for i in self.async_ask_stream_clicking(prompt,remaining_retry=remaining_retry-1):
                        yield i
                    return

                logger.info("< (ChatGPT) %s",chunk)
                yield chunk

            # if we saw the eof signal, this was the last event we
            # should process and we are done
            if len(eof_datas) > 0:
                break

            await asyncio.sleep(0.2)
        else:
            # Timeout
            logger.error("Timed out waiting for ChatGPT response. ")
            await self.page.reload()
            await asyncio.sleep(10)
            async for i in self.async_ask_stream_clicking(prompt,remaining_retry=remaining_retry-1):
                yield i
            return

        await self._cleanup_divs()


    async def async_ask(self, message: str, remaining_retry=2) -> str:
        """
        Send a message to chatGPT and return the response.

        Args:
            message (str): The message to send.
            remaining_retry (int): How many retries left

        Returns:
            str: The response received from OpenAI.
        """
        if remaining_retry==0:
            logger.error("Cannot ask ChatGPT. Repeatedly receiving empty response. ")
            raise error.ChatGPTResponseError("Cannot ask ChatGPT. Repeatedly receiving empty response. ")

        response = [i async for i in self.async_ask_stream(message)]
        if len(response)!=0:
            return ''.join(response)
        # Else: retry
        logger.error("Received empty response from ChatGPT. Retrying... ")
        await self.async_refresh_session()
        return await self.async_ask(message,remaining_retry=remaining_retry-1)

    async def async_new_conversation(self):
        self.parent_message_id = str(uuid.uuid4())
        self.conversation_id = None



async def main():
    gpt=await AsyncChatGPT.create(headless=False)
    # loop=asyncio.get_event_loop()
    # prompt=input("> ")
    prompt="Good night!"
    while prompt!='exit':
        prompt=input("> ")
        print('< ',end='',flush=True)
        print(await gpt.async_ask(prompt))
        print('')

if __name__=='__main__':
    asyncio.run(main())

