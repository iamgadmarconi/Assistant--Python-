
 # ==== file path: agent\..\src\agent\agent.py ==== 

import os
import asyncio
import json
from aiofiles import open as aio_open
from pathlib import Path
from openai import OpenAI
from src.utils.files import load_from_toml, read_to_string, list_files, bundle_to_file, load_from_json, load_to_json, ensure_dir, db_to_json
from src.ais.assistant import load_or_create_assistant, upload_instruction, upload_file_by_name, get_thread, create_thread, run_thread_message


class Assistant:

    def __init__(self, directory) -> None:
        self.dir = directory

    async def init_from_dir(self, recreate: bool = False):
        self.config = load_from_toml(f"{self.dir}/agent.toml")
        self.oac = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.asst_id = await load_or_create_assistant(self.oac, self.config, recreate)
        self.name = self.config["name"]

        db_to_json()
        await upload_file_by_name(self.oac, self.asst_id, Path(r"agent\.agent\persistance\memory.json"), recreate)

        await self.upload_instructions()
        await self.upload_files(recreate)

        return self

    async def upload_instructions(self):
        file_path = os.path.join(self.dir, self.config["instructions_file"])
        if os.path.exists(file_path):
            async with aio_open(file_path, 'r') as file:
                inst_content = await file.read()
            await upload_instruction(self.oac, self.config, self.asst_id, inst_content)  # Assuming an async upload_instruction function
            return True
        else:
            return False
        
    async def upload_files(self, recreate: bool) -> int:
        num_uploaded = 0

        data_files_dir = self.data_files_dir()

        exclude_element = f"*{self.asst_id}*"
        
        for file in list_files(data_files_dir, ["*.rs", "*.md"], [exclude_element]):

            if ".agent" not in str(file):
                raise Exception(f"File '{file}' is not in data directory")

            os.remove(file)

        for bundle in self.config["file_bundles"]:
            src_dir = Path(self.dir).joinpath(bundle['src_dir'])

            if src_dir.is_dir():
                src_globs = bundle['src_globs']

                files = list_files(src_dir, src_globs, None)

                if files:
                    bundle_file_name = f"{self.name}-{bundle['bundle_name']}-{self.asst_id}.{bundle['dst_ext']}"
                    bundle_file = self.data_files_dir().joinpath(bundle_file_name)

                    force_reupload = recreate or not bundle_file.exists()

                    # Assuming bundle_to_file bundles files into the specified bundle_file
                    bundle_to_file(files, bundle_file)

                    _, uploaded = await upload_file_by_name(self.oac, self.asst_id, bundle_file, force_reupload)
                    if uploaded:
                        num_uploaded += 1

        return num_uploaded
    
    async def load_or_create_conv(self, recreate: bool):
        conv_file = Path(self.data_dir()).joinpath("conv.json")

        if recreate and conv_file.exists():
            os.remove(conv_file)  # Removes the file

        try:
            conv = load_from_json(conv_file)
            await get_thread(self.oac, conv['thread_id'])
            print(f"Conversation loaded")  

        except (FileNotFoundError, json.JSONDecodeError):
            thread_id = await create_thread(self.oac)
            print(f"Conversation created")  
            conv = {'thread_id': thread_id}
            load_to_json(conv_file, conv)

        return conv
    
    async def chat(self, conv, msg: str) -> str:
        res = await run_thread_message(self.oac, self.asst_id, conv["thread_id"], msg)
        return res
    
    def data_dir(self) -> Path:
        """Returns the path to the data directory, ensuring its existence."""
        data_dir = Path(self.dir + "\.agent")
        ensure_dir(data_dir)
        return data_dir

    def data_files_dir(self) -> Path:
        """Returns the path to the data files directory within the data directory, ensuring its existence."""
        files_dir = self.data_dir().joinpath("files")
        ensure_dir(files_dir)
        return files_dir


 # ==== file path: agent\..\src\agent\__init__.py ==== 




 # ==== file path: agent\..\src\ais\assistant.py ==== 

import asyncio
import backoff
from time import sleep
from openai import NotFoundError
from pathlib import Path


from src.ais.msg import get_text_content, user_msg
from src.utils.database import write_to_memory


async def create(client, config):
    assistant = client.beta.assistants.create(
        name = config["name"],
        model = config["model"],
        tools = config["tools"],
    )

    return assistant

async def load_or_create_assistant(client, config, recreate: bool = False):
    asst_obj = await first_by_name(client, config["name"])

    asst_id = asst_obj.id if asst_obj is not None else None

    if recreate and asst_id is not None:
        await delete(client, config, asst_id)
        asst_id = None 
        print(f"Assistant '{config['name']}' deleted")

    if asst_id is not None:
        print(f"Assistant '{config['name']}' loaded")
        return asst_id
    
    else:
        asst_obj = await create(client, config)
        print(f"Assistant '{config['name']}' created")
        return asst_obj.id

async def first_by_name(client, name: str):
    assts = client.beta.assistants
    assistants = assts.list().data
    asst_obj =  next((asst for asst in assistants if asst.name == name), None)
    return asst_obj

@backoff.on_exception(backoff.expo,
                    NotFoundError,
                    max_tries=5,
                    giveup=lambda e: "No assistant found" not in str(e))
async def upload_instruction(client, config, asst_id: str, instructions: str):
    assts = client.beta.assistants
    try: 
        assts.update(
            assistant_id= asst_id,
            instructions = instructions
        )
        print(f"Instructions uploaded to assistant '{config['name']}'")

    except Exception as e:
        print(f"Failed to upload instruction: {e}")
        raise  

async def delete(client, config, asst_id: str):
    assts = client.beta.assistants 
    assistant_files = client.files

    file_hashmap = await get_file_hashmap(client, asst_id)

    for file_id in file_hashmap.values():
        del_res = assistant_files.delete(file_id)

        if del_res.deleted:
            print(f"File '{file_id}' deleted")
        
    assts.delete(assistant_id=asst_id)
    print(f"Assistant '{config['name']}' deleted")

async def get_file_hashmap(client, asst_id: str):
    assts = client.beta.assistants
    assistant_files = assts.files.list(assistant_id=asst_id).data
    asst_file_ids = {file.id for file in assistant_files}

    org_files = client.files.list().data
    file_id_by_name = {org_file.filename: org_file.id for org_file in org_files if org_file.id in asst_file_ids}
    
    return file_id_by_name

async def create_thread(client):
    threads = client.beta.threads
    res = threads.create()
    return res.id

async def get_thread(client, thread_id: str):
    threads = client.beta.threads
    res = threads.retrieve(thread_id)
    return res

async def run_thread_message(client, asst_id: str, thread_id: str, message: str):

    msg = user_msg(message)

    threads = client.beta.threads
    _message_obj = threads.messages.create(
        thread_id=thread_id,
        content=message,
        role="user",
    )

    run = threads.runs.create(
        thread_id=thread_id,
        assistant_id=asst_id,
    )

    write_to_memory(r"agent\.agent\persistance\memory.db", "User", message)

    while True:
            print("-", end="", flush=True)
            run = threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

            if run.status in ["Completed", "completed"]:
                print("\n")  # Move to the next line
                return await get_thread_message(client, thread_id)
            elif run.status in ["Queued", "InProgress", "run_in_progress", "in_progress", "queued"]:
                pass  
            else:
                print("\n")  # Move to the next line
                # Raising an exception for unexpected status
                raise Exception(f"Unexpected run status: {run.status}")

            sleep(0.5)

async def get_thread_message(client, thread_id: str):
    threads = client.beta.threads
    
    try:
        messages = threads.messages.list(
            thread_id=thread_id,
            order="desc",
            extra_query={"limit": "1"},
        ).data

        msg = next(iter(messages), None)
        if msg is None:
            raise ValueError("No message found in thread")
        txt = get_text_content(msg)

        write_to_memory(r"agent\.agent\persistance\memory.db", "Assistant", txt)

        return txt
    
    except Exception as e:
        raise ValueError(f"An error occurred: {str(e)}")

async def upload_file_by_name(client, asst_id: str, filename: str, force: bool = False):
    assts = client.beta.assistants
    assistant_files = assts.files
    
    file_id_by_name = await get_file_hashmap(client, asst_id)

    file_id = file_id_by_name.pop(filename.name, None)

    if not force:
        if file_id is not None:
            print(f"File '{filename}' already uploaded")
            return file_id, False
    
    if file_id:
        try:
            assistant_files.delete(file_id)

        except Exception as e:
            print(f"Failed to delete file '{filename}': {e}")
            raise

        try:
            assts.files.delete(
                assistant_id=asst_id,
                file_id=file_id,
            )
        
        except Exception as e:
            print(f"Couldn't remove assistant file '{filename}': {e}")
            raise

    with open(filename, "rb") as file:
        uploaded_file = client.files.create(
            file=file,
            purpose="assistants",
        )

    assistant_files.create(
        assistant_id=asst_id,
        file_id=uploaded_file.id,
    )

    print(f"File '{filename}' uploaded")
    return uploaded_file.id, True




 # ==== file path: agent\..\src\ais\functions.py ==== 




 # ==== file path: agent\..\src\ais\msg.py ==== 

# def pretty_print(messages):
#     print(messages)
#     for m in messages:
#         print(f"{m.role}: {m.content[0].text.value}")
#     print("\n")

# def pretty_print(messages):
#     m = messages
#     # print(f"{m.role}: {m.content[0].text.value}")
#     # print("\n")
#     print(m)

# def get_text_content(messages):
#     msg_content = [m.content[0].text.value for m in messages]
    
#     if not msg_content:
#         return "No messages found"

#     return msg_content

class CreateMessageRequest:
    def __init__(self, role, content, **kwargs):
        self.role = role
        self.content = content
        for key, value in kwargs.items():
            setattr(self, key, value)

class MessageContentText:
    def __init__(self, text):
        self.text = text

class MessageContentImageFile:
    pass

class MessageObject:
    def __init__(self, content):
        self.content = content

def user_msg(content):
    return CreateMessageRequest(
        role="user",
        content=str(content),  # Converts the content into a string
    )

def get_text_content(msg):
    if not msg.content:
        raise ValueError("No content found in message")

    msg_content = next(iter(msg.content), None)

    if msg_content is None:
        raise ValueError("No content found in message")

    if isinstance(msg_content.text.value, str):
        return msg_content.text.value
    else:
        raise ValueError("Unsupported message content type")



 # ==== file path: agent\..\src\ais\__init__.py ==== 




 # ==== file path: agent\..\src\utils\database.py ==== 

import sqlite3
import os
import datetime

def create_or_load_db() -> sqlite3.Connection:
    if not os.path.exists(r"agent\.agent\persistance\memory.db"):
        con = sqlite3.connect(r"agent\.agent\persistance\memory.db")
        cur = con.cursor()

        cur.execute("CREATE TABLE IF NOT EXISTS memory (role TEXT, time TEXT, message TEXT)")
    
    return sqlite3.connect(r"agent\.agent\persistance\memory.db")

def write_to_memory(file, role, val):

    con = create_or_load_db(file)
    cur = con.cursor()
    cur.execute("INSERT INTO memory (role, time, message) VALUES (?, ?, ?)", (role, datetime.datetime.now(), val))
    con.commit()
    con.close()



 # ==== file path: agent\..\src\utils\files.py ==== 


import tomli
import os
import json
import fnmatch
from pathlib import Path
from typing import TypeVar, Generic, List, Optional
from src.utils.database import create_or_load_db

T = TypeVar('T')

def load_from_toml(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"File '{path}' not found")
    
    with open(path, 'rb') as f:  # TOML files should be opened in binary mode for `tomli`
        data = tomli.load(f)

    return data


def read_to_string(path: str) -> str:
    if not os.path.exists(path):
        raise FileNotFoundError(f"File '{path}' not found")
    
    with open(path, 'r') as f:
        data = f.read()

    return data

def load_from_json(file: Path) -> T:
    with open(file, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_to_json(file: Path, val: T) -> None:
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(val, f, indent=4)

def ensure_dir(dir_path: Path) -> bool:
    if dir_path.is_dir():
        return False
    else:
        dir_path.mkdir(parents=True, exist_ok=True)
        return True

def base_dir_exclude_globs(directory_path: Path, exclude_patterns=None):
    if exclude_patterns is None:
        exclude_patterns = ['.git*', 'target*']
    return get_glob_set(directory_path, exclude_patterns)

def get_glob_set(directory_path: Path, globs):
    matched_files = []
    for root, dirs, files in os.walk(directory_path):
        for file in files + dirs:
            full_path = Path(root) / file
            if any(fnmatch.fnmatch(str(full_path), pattern) for pattern in globs):
                matched_files.append(full_path)
    return matched_files

def bundle_to_file(files, dst_file):
    try:
        with open(dst_file, 'w', encoding='utf-8') as writer:

            for file_path in files:
                file_path = Path(file_path)

                if not file_path.is_file():
                    raise FileNotFoundError(f"File not found: {file_path}")
                
                with open(file_path, 'r', encoding='utf-8') as reader:
                    writer.write(f"\n # ==== file path: {file_path} ==== \n\n")

                    for line in reader:
                        writer.write(f"{line}")

                    writer.write("\n\n")

    except Exception as e:
        return f"An error occurred: {e}"
    
    return "Success"

def list_files(dir_path: Path, include_globs: Optional[List[str]] = None, exclude_globs: Optional[List[str]] = None) -> List[Path]:
    def is_match(path: Path, patterns: Optional[List[str]]) -> bool:
        if patterns is None:
            return True
        return any(fnmatch.fnmatch(str(path), pattern) for pattern in patterns)

    # Determine depth based on include globs (simplified approach)
    depth = 100 if include_globs and any("**" in glob for glob in include_globs) else 1

    matched_files = []
    for root, dirs, files in os.walk(dir_path):
        # Calculate current depth
        current_depth = len(Path(root).parts) - len(dir_path.parts)
        if current_depth > depth:
            # Stop diving into directories if max depth is exceeded
            del dirs[:]
            continue

        paths = [Path(root) / file for file in files]
        for path in paths:
            # Exclude directories or files based on exclude_globs
            if exclude_globs and is_match(path, exclude_globs):
                continue
            # Include files based on include_globs
            if is_match(path, include_globs):
                matched_files.append(path)
                
    return matched_files

def db_to_json():

    con = create_or_load_db()
    cur = con.cursor()

    cur.execute("SELECT * FROM memory")

    rows = cur.fetchall()

    data = {}

    for role, date, message in rows:
        if role not in data:
            data[role] = {}
        data[role][date] = {"message": message}

    con.close()

    with open(r"agent\.agent\persistance\memory.json", "w") as f:
        json.dump(data, f, indent=4)


 # ==== file path: agent\..\src\utils\__init__.py ==== 



