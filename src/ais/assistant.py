import asyncio
import backoff
import json
import os
import re
import base64

from openai import NotFoundError
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.ais.msg import get_text_content, user_msg
from src.utils.database import write_to_memory
from src.utils.files import find
from src.utils.cli import red_text, green_text, yellow_text

from src.ais.functions.azure import getCalendar, readEmail, writeEmail, sendEmail, writeCalendarEvent, createCalendarEvent, getContacts
from src.ais.functions.misc import getWeather, getLocation, getDate
from src.ais.functions.office import csvQuery


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
        await delete(client, asst_id)
        asst_id = None
        green_text(f"Assistant '{config['name']}' deleted")
        # print(f"Assistant '{config['name']}' deleted")

    if asst_id is not None:
        green_text(f"Assistant '{config['name']}' loaded")
        # print(f"Assistant '{config['name']}' loaded")
        return asst_id
    
    else:
        asst_obj = await create(client, config)
        green_text(f"Assistant '{config['name']}' created")
        # print(f"Assistant '{config['name']}' created")
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
        # print(f"Instructions uploaded to assistant '{config['name']}'")
        green_text(f"Instructions uploaded to assistant '{config['name']}'")

    except Exception as e:
        red_text(f"Failed to upload instruction: {e}")
        # print(f"Failed to upload instruction: {e}")
        raise  

async def delete(client, asst_id: str, wipe=False):
    assts = client.beta.assistants 
    assistant_files = client.files

    file_hashmap = await get_file_hashmap(client, asst_id)

    for file_id in file_hashmap.values():
        del_res = assistant_files.delete(file_id)

        if del_res.deleted:
            green_text(f"File '{file_id}' deleted")
            # print(f"File '{file_id}' deleted")

    for key in file_hashmap.keys():
        path = find(key, "agent")
        if path:
            if os.path.exists(path):
                os.remove(path)

    try:
        if os.path.exists(find("memory.json", "agent")):
            os.remove(find("memory.json", "agent"))
    except:
        pass
    
    try:
        if wipe:
            if os.path.exists(find("memory.db", "agent")):
                os.remove(find("memory.db", "agent"))
    except:
        pass

    assts.delete(assistant_id=asst_id)
    # print(f"Assistant deleted")
    green_text("Assistant deleted")

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

    pattern = r"run_[a-zA-Z0-9]+"

    try:
        _message_obj = threads.messages.create(
            thread_id=thread_id,
            content=message,
            role="user",
        )

        run = threads.runs.create(
            thread_id=thread_id,
            assistant_id=asst_id,
        )

    except Exception as e:
        match = re.search(pattern, str(e.message))

        if match:
            run_id = match.group()
            run = threads.runs.retrieve(thread_id=thread_id, run_id=run_id)

        else:
            
            raise e
        
    write_to_memory("User", message)

    with Progress(SpinnerColumn(), TextColumn("[bold cyan]{task.description}"), transient=True) as progress:
        task = progress.add_task("[green]Thinking...", total=None)  # Indeterminate progress
        
        while True:
                # print("-", end="", flush=True)
                run = threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

                if run.status in ["Completed", "completed"]:
                    progress.stop()
                    print()
                    return await get_thread_message(client, thread_id)
                
                elif run.status in ["Queued", "InProgress", "run_in_progress", "in_progress", "queued", "pending", "Pending"]:
                    pass  # The spinner will continue spinning

                elif run.status in ['requires_input', 'RequiresInput', 'requires_action', 'RequiresAction']:
                    await call_required_function(client, thread_id, run.id, run.required_action)

                else:
                    print() 
                    await delete(client, asst_id)
                    # print(f"Unexpected run status: {run.status}")
                    red_text(f"Unexpected run status: {run.status}")
                    raise

                await asyncio.sleep(0.5)

async def call_required_function(client, thread_id: str, run_id: str, required_action):
    tool_outputs = []

    for action in required_action:
        if not isinstance(action[1], str):
            
            func_name = action[1].tool_calls[0].function.name
            args = json.loads(action[1].tool_calls[0].function.arguments)
            
            if func_name == "getWeather":
                outputs = getWeather(
                    msg = args.get("msg", None)
                )
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )
            elif func_name == "getCalendar":
                outputs = getCalendar(
                    upto = args.get("upto", None)
                )
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )
            
            elif func_name == "readEmail":
                outputs = readEmail(
                )
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )

            elif func_name == "writeEmail":
                outputs = writeEmail(
                    recipients=args.get("recipients", None),
                    subject = args.get("subject", None),
                    body = args.get("body", None),
                    attachments = args.get("attachments", None)
                )
                
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )
            
            elif func_name == "sendEmail":
                outputs = sendEmail(
                    recipients=args.get("recipients", None),
                    subject = args.get("subject", None),
                    body = args.get("body", None),
                    attachments = args.get("attachments", None)
                )
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )

            elif func_name == "getLocation":
                outputs = getLocation()
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )
            
            elif func_name == "getDate":
                outputs = getDate()
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )

            elif func_name == "writeCalendarEvent":
                outputs = writeCalendarEvent(
                    subject = args.get("subject", None),
                    start = args.get("start", None),
                    end = args.get("end", None),
                    location = args.get("location", None),
                    body = args.get("body", None),
                    recurrence = args.get("recurrence", False)
                )
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )

            elif func_name == "createCalendarEvent":
                outputs = createCalendarEvent(
                    subject = args.get("subject", None),
                    start = args.get("start", None),
                    end = args.get("end", None),
                    location = args.get("location", None),
                    body = args.get("body", None),
                    recurrence = args.get("recurrence", False)
                )
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )

            elif func_name == "getContacts":
                outputs = getContacts(
                    name = args.get("name", None)
                )
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )
            
            elif func_name == "csvQuery":
                outputs = csvQuery(
                    path = args.get("path", None),
                    query = args.get("query", None)
                )
                tool_outputs.append(
                    {
                        "tool_call_id": action[1].tool_calls[0].id,
                        "output": outputs
                    }
                )
                
            else:
                raise ValueError(f"Function '{func_name}' not found")
            
    # print(f"debug-- tool_outputs: {tool_outputs}\n\n")

    for tool_output in tool_outputs:
        if isinstance(tool_output['output'], bytes):
            tool_output['output'] = "[bytes]" + base64.b64encode(tool_output['output']).decode("utf-8") + "[/bytes]"

    # print(f"debug-- tool_outputs after encoding: {tool_outputs}\n\n")

    client.beta.threads.runs.submit_tool_outputs(
        thread_id=thread_id,
        run_id=run_id,
        tool_outputs=tool_outputs,
    )

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

        txt = get_text_content(client, msg)

        write_to_memory("Assistant", txt)

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
            # print(f"File '{filename}' already uploaded")
            yellow_text(f"File '{filename}' already uploaded")
            return file_id, False
    
    if file_id:
        try:
            assistant_files.delete(
                assistant_id=asst_id,
                file_id=file_id
            )

        except Exception as e:
            # print(f"Failed to delete file '{filename}': {e}")
            red_text(f"Failed to delete file '{filename}': {e}")
            raise

        try:
            assts.files.delete(
                assistant_id=asst_id,
                file_id=file_id,
            )
        
        except:
            try:
                yellow_text(f"Couldn't remove assistant file '{filename}', trying again...")
                client.files.delete(file_id)
                green_text(f"File '{filename}' removed")
            except Exception as e:
                # print(f"Couldn't remove assistant file '{filename}': {e}")
                red_text(f"Couldn't remove assistant file '{filename}: {e}'")
                raise

    with open(filename, "rb") as file:
        uploaded_file = client.files.create(
            file=file,
            purpose="assistants",
        )
    try:
        assistant_files.create(
            assistant_id=asst_id,
            file_id=uploaded_file.id,
        )

        green_text(f"File '{filename}' uploaded")
        # print(f"File '{filename}' uploaded")
        return uploaded_file.id, True
    
    except Exception as e:
        # print(f"Failed to upload file '{filename}': {e}")
        red_text(f"\nFailed to upload file '{filename}': {e}\n")
        yellow_text(f"This can be a bug with the OpenAI API. Please check the storage at https://platform.openai.com/storage or try again")
        return None, False

