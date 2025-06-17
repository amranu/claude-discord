import os
import asyncio
import logging
import logging.handlers
import json
import subprocess
import aiohttp
import tempfile
from typing import Optional
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
#from claude_code_sdk import query, ClaudeCodeOptions

load_dotenv()

# Set Claude CLI path in environment
os.environ["PATH"] = f"/usr/local/bin/claude:{os.environ.get('PATH', '')}"

# Setup logging with file output and rotating logs
log_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Create logs directory if it doesn't exist
os.makedirs('logs', exist_ok=True)

# Setup rotating file handler for general logs
file_handler = logging.handlers.RotatingFileHandler(
    'logs/discord_bot.log',
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

# Setup rotating file handler for Claude stream logs (unparsed)
claude_stream_handler = logging.handlers.RotatingFileHandler(
    'logs/claude_stream.log',
    maxBytes=50*1024*1024,  # 50MB for large streams
    backupCount=10
)
claude_stream_formatter = logging.Formatter('%(asctime)s - %(message)s')
claude_stream_handler.setFormatter(claude_stream_formatter)
claude_stream_handler.setLevel(logging.INFO)

# Setup console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)

logger = logging.getLogger(__name__)

# Create separate logger for Claude streams
claude_stream_logger = logging.getLogger('claude_stream')
claude_stream_logger.setLevel(logging.INFO)
claude_stream_logger.addHandler(claude_stream_handler)
claude_stream_logger.propagate = False  # Don't send to root logger

CLAUDE_CLI_PATH = "/usr/local/bin/claude"

class ClaudeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        
    async def on_ready(self):
        logger.info(f'{self.user} has connected to Discord!')
        
    async def on_message(self, message):
        if message.author == self.user:
            return
            
        await self.process_commands(message)

bot = ClaudeBot()

# Global variable to track running Claude subprocess
current_claude_process = None
current_claude_channel = None

def format_usage_limit_message(message: str) -> str:
    """Convert usage limit message with unix timestamp to human readable format"""
    if "Claude AI usage limit reached|" in message:
        try:
            # Extract timestamp from message like "Claude AI usage limit reached|1750017600"
            timestamp_str = message.split("|")[1]
            timestamp = int(timestamp_str)
            
            # Convert to datetime
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            
            # Format as readable string
            formatted_date = dt.strftime("%B %d, %Y at %H:%M UTC")
            
            return f"🚫 **Claude AI Usage Limit Reached**\n⏰ Limit resets on {formatted_date}"
        except (IndexError, ValueError, OSError) as e:
            logger.error(f"Error parsing usage limit timestamp: {e}")
            return "🚫 **Claude AI Usage Limit Reached**\n⏰ Please try again later"
    return message

def format_todo_content(content: str) -> str:
    """Format todo list content for Discord display"""
    try:
        import json
        import re
        
        # Handle simple messages first
        if "Todos have been modified successfully" in content:
            return "📋 **Todo List Updated Successfully**"
        
        if "Remember to continue to use" in content and "todo list" in content.lower():
            # Extract JSON from the TodoRead response
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                todos_json = json_match.group()
                todos = json.loads(todos_json)
                return format_todos_list(todos)
            else:
                return "📋 **Todo List:** Error parsing content"
        
        # Try to extract JSON array from the content
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if json_match:
            todos_json = json_match.group()
            todos = json.loads(todos_json)
            return format_todos_list(todos)
        else:
            # Fallback for non-JSON todo content
            if "todo list" in content.lower():
                return f"📋 **Todo Update:** {content}"
            return f"`{content}`"
            
    except Exception as e:
        # Fallback to original content if parsing fails
        return f"📋 **Todo Error:** `{content}`"

def format_todos_list(todos: list) -> str:
    """Format a list of todos for Discord display"""
    if not todos:
        return "📋 **Todo List:** Empty"
    
    formatted = "📋 **Todo List:**\n"
    
    # Group by status
    in_progress = [t for t in todos if t.get('status') == 'in_progress']
    pending = [t for t in todos if t.get('status') == 'pending']
    completed = [t for t in todos if t.get('status') == 'completed']
    
    if in_progress:
        formatted += "\n🔄 **In Progress:**\n"
        for todo in in_progress:
            priority_emoji = "🔴" if todo.get('priority') == 'high' else "🟡" if todo.get('priority') == 'medium' else "🟢"
            formatted += f"  {priority_emoji} {todo.get('content', 'Unknown task')}\n"
    
    if pending:
        formatted += "\n⏳ **Pending:**\n"
        for todo in pending:
            priority_emoji = "🔴" if todo.get('priority') == 'high' else "🟡" if todo.get('priority') == 'medium' else "🟢"
            formatted += f"  {priority_emoji} {todo.get('content', 'Unknown task')}\n"
    
    if completed:
        formatted += "\n✅ **Completed:**\n"
        for todo in completed:
            formatted += f"  ✓ {todo.get('content', 'Unknown task')}\n"
    
    return formatted.strip()

async def download_and_read_attachment(attachment: discord.Attachment) -> str:
    """Download and read the content of a Discord attachment"""
    try:
        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{attachment.filename}") as temp_file:
            temp_path = temp_file.name
        
        # Download the attachment
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as response:
                if response.status == 200:
                    content = await response.read()
                    with open(temp_path, 'wb') as f:
                        f.write(content)
                else:
                    return f"Error downloading {attachment.filename}: HTTP {response.status}"
        
        # Try to read as text file
        try:
            with open(temp_path, 'r', encoding='utf-8') as f:
                file_content = f.read()
            
            # Clean up temp file
            os.unlink(temp_path)
            
            return f"**File: {attachment.filename}**\n```\n{file_content}\n```"
            
        except UnicodeDecodeError:
            # If it's not a text file, return file info
            file_size = os.path.getsize(temp_path)
            os.unlink(temp_path)
            return f"**File: {attachment.filename}** (Binary file, {file_size} bytes) - Cannot display content as text"
            
    except Exception as e:
        logger.error(f"Error processing attachment {attachment.filename}: {e}")
        return f"Error processing {attachment.filename}: {str(e)}"

async def send_long_message(ctx, message: str, max_length: int = 2000):
    """Split and send long messages in chunks"""
    if len(message) <= max_length:
        await ctx.send(message)
        return
    
    continuation_prefix = ""
    
    def split_text(text: str, max_len: int) -> list:
        """Recursively split text into chunks that fit within max_len"""
        if len(text) <= max_len:
            return [text]
        
        # Try to split by lines first
        lines = text.split('\n')
        if len(lines) > 1:
            chunks = []
            current = ""
            
            for line in lines:
                test_line = current + line + "\n" if current else line + "\n"
                if len(test_line) <= max_len:
                    current = test_line
                else:
                    if current:
                        chunks.append(current.rstrip())
                        current = line + "\n"
                        if len(current) > max_len:
                            # Line itself is too long, split by words
                            chunks.extend(split_text(line, max_len))
                            current = ""
                    else:
                        # Single line too long, split by words
                        chunks.extend(split_text(line, max_len))
            
            if current:
                chunks.append(current.rstrip())
            
            return chunks
        
        # Split by words
        words = text.split(' ')
        if len(words) > 1:
            chunks = []
            current = ""
            
            for word in words:
                test_text = current + word + " " if current else word + " "
                if len(test_text) <= max_len:
                    current = test_text
                else:
                    if current:
                        chunks.append(current.rstrip())
                        current = word + " "
                        if len(current) > max_len:
                            # Single word too long, truncate it
                            chunks.append(word[:max_len-3] + "...")
                            current = ""
                    else:
                        # Single word too long, truncate it
                        chunks.append(word[:max_len-3] + "...")
            
            if current:
                chunks.append(current.rstrip())
            
            return chunks
        
        # Single word/text too long, truncate
        return [text[:max_len-3] + "..."]
    
    # Split the message into chunks
    chunks = split_text(message, max_length)
    
    # Send all chunks
    for chunk in chunks:
        await ctx.send(chunk)

class ActivityTimeout:
    """Manages timeout that resets on activity"""
    def __init__(self, base_timeout: float = 300.0):
        self.base_timeout = base_timeout
        self.last_activity = asyncio.get_event_loop().time()
        self.start_time = self.last_activity
        
    def reset(self):
        """Reset the activity timeout"""
        self.last_activity = asyncio.get_event_loop().time()
        
    def time_remaining(self) -> float:
        """Get remaining time before timeout"""
        current_time = asyncio.get_event_loop().time()
        elapsed_since_activity = current_time - self.last_activity
        return max(0, self.base_timeout - elapsed_since_activity)
        
    def is_expired(self) -> bool:
        """Check if timeout has expired"""
        return self.time_remaining() <= 0

async def call_claude_enhanced(prompt: str, system_prompt: str = None, tools: list = None, 
                             continue_conversation: bool = False, resume_session: str = None, ctx=None) -> str:
    """Enhanced Claude CLI call that handles streaming responses properly"""
    global current_claude_process, current_claude_channel
    try:
        cmd = [
            CLAUDE_CLI_PATH,
            "--output-format", "stream-json",
            "--verbose",
            "--print", prompt
        ]
        
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
            
        if tools:
            cmd.extend(["--allowedTools", ",".join(tools)])
            
        if continue_conversation:
            cmd.append("--continue")
            
        if resume_session:
            cmd.extend(["--resume", resume_session])
        
        # Run subprocess with streaming and better error handling
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024*1024*10  # 10MB buffer limit
            )
            # Store process reference globally for stop command
            current_claude_process = process
            current_claude_channel = ctx.channel if ctx else None
        except Exception as e:
            logger.error(f"Failed to start Claude CLI process: {e}")
            return f"Error: Failed to start Claude CLI: {str(e)}"
        
        # Send empty input to stdin and close it
        if process.stdin:
            process.stdin.write(b'\n')
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
        
        response_parts = []
        error_parts = []
        
        # Create activity timeout tracker
        activity_timeout = ActivityTimeout(300.0)  # 5 minute base timeout
        
        async def read_stdout():
            """Read and parse stdout stream continuously with true streaming"""
            last_send_time = asyncio.get_event_loop().time()
            accumulated_text = ""
            current_assistant_message = ""
            sent_text_length = 0  # Track how much text we've already sent
            last_discord_message = None
            partial_line = ""
            tools_used_after_text = False  # Track if tools were used after text was sent
            
            while True:
                try:
                    if process.stdout is None:
                        break
                    
                    # Read smaller chunks for true streaming with timeout
                    try:
                        chunk = await asyncio.wait_for(process.stdout.read(1024), timeout=5.0)
                    except asyncio.TimeoutError:
                        # No data for 5 seconds, check if process is still alive
                        if process.returncode is not None:
                            break
                        continue
                    except Exception as e:
                        logger.error(f"Error reading stdout chunk: {e}")
                        break
                    
                    if not chunk:
                        # Stream ended - check if process is still running
                        if process.returncode is not None:
                            # Process has terminated - send any remaining text
                            if current_assistant_message and ctx:
                                if last_discord_message and not tools_used_after_text:
                                    # Send remaining text that hasn't been sent yet
                                    remaining_text = current_assistant_message[sent_text_length:]
                                    if remaining_text.strip():
                                        await send_long_message(ctx, remaining_text)
                                        sent_text_length = len(current_assistant_message)
                                        activity_timeout.reset()  # Reset timeout
                                else:
                                    # Send only remaining text if tools were used
                                    if tools_used_after_text and sent_text_length < len(current_assistant_message):
                                        remaining_text = current_assistant_message[sent_text_length:]
                                        if remaining_text.strip():
                                            await send_long_message(ctx, remaining_text)
                                            sent_text_length = len(current_assistant_message)  # Update tracking
                                            last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                    elif not tools_used_after_text and sent_text_length == 0:
                                        # Send full message only if nothing has been sent yet
                                        await send_long_message(ctx, current_assistant_message)
                                        sent_text_length = len(current_assistant_message)  # Update tracking
                                        last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                    elif not tools_used_after_text and sent_text_length < len(current_assistant_message):
                                        # Send only remaining text if some was already sent
                                        remaining_text = current_assistant_message[sent_text_length:]
                                        if remaining_text.strip():
                                            await send_long_message(ctx, remaining_text)
                                            sent_text_length = len(current_assistant_message)  # Update tracking
                                            last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                            elif accumulated_text and ctx:
                                await send_long_message(ctx, accumulated_text)
                                last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                            break
                        # Process still running, wait a bit and continue
                        await asyncio.sleep(0.1)
                        continue
                    
                    chunk_text = chunk.decode('utf-8', errors='ignore')
                    
                    # Log the raw unparsed stream chunk
                    claude_stream_logger.info(f"RAW_CHUNK: {repr(chunk_text)}")
                    
                    partial_line += chunk_text
                    
                    # Process complete lines
                    while '\n' in partial_line:
                        line, partial_line = partial_line.split('\n', 1)
                        
                        # Log every complete line (unparsed)
                        claude_stream_logger.info(f"RAW_LINE: {repr(line)}")
                        
                        line = line.strip()
                        if not line:
                            continue
                            
                        try:
                            data = json.loads(line)
                            msg_type = data.get("type")
                            
                            if msg_type == "assistant" and "message" in data:
                                # Handle assistant messages - extract text and stream it
                                for block in data["message"].get("content", []):
                                    if block.get("type") == "text":
                                        text = block.get("text", "")
                                        if text:
                                            current_assistant_message += text
                                            response_parts.append(text)
                                            
                                            # Stream assistant responses in real-time
                                            if ctx:
                                                current_time = asyncio.get_event_loop().time()
                                                
                                                # Update Discord message every 1 second or every 500 chars
                                                if ((current_time - last_send_time >= 1.0) or 
                                                    (len(current_assistant_message) % 500 < len(text))):
                                                    
                                                    message_to_send = current_assistant_message[:2000]  # Discord limit
                                                    if len(current_assistant_message) > 2000:
                                                        message_to_send = message_to_send[:-3] + "..."
                                                    
                                                    try:
                                                        # Always send only new text, never edit
                                                        new_text = current_assistant_message[sent_text_length:]
                                                        if new_text.strip():
                                                            last_discord_message = await send_long_message(ctx, new_text)
                                                            sent_text_length = len(current_assistant_message)
                                                            tools_used_after_text = False  # Reset for future text
                                                            last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                                    except discord.errors.HTTPException:
                                                        # If edit fails, send new message
                                                        if tools_used_after_text:
                                                            new_text = current_assistant_message[sent_text_length:]
                                                            if new_text.strip():
                                                                last_discord_message = await ctx.send(new_text[:2000])
                                                                sent_text_length = len(current_assistant_message)
                                                                last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                                        else:
                                                            last_discord_message = await ctx.send(message_to_send)
                                                            sent_text_length = len(current_assistant_message)
                                                            last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                                        tools_used_after_text = False
                                                    except Exception as e:
                                                        logger.error(f"Error updating Discord message: {e}")
                                                    
                                                    last_send_time = current_time
                                    
                                    elif block.get("type") == "thinking":
                                        # Display thinking content
                                        thinking_content = block.get("thinking", "")
                                        if thinking_content and ctx:
                                            # Truncate thinking if too long for Discord
                                            if len(thinking_content) > 1800:
                                                thinking_preview = thinking_content[:1800] + "..."
                                            else:
                                                thinking_preview = thinking_content
                                            
                                            thinking_msg = f"💭 **Claude's Thinking:**\n```\n{thinking_preview}\n```"
                                            await ctx.send(thinking_msg)
                                            last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                    
                                    elif block.get("type") == "tool_use":
                                        # Mark that tools are being used after text was sent
                                        if last_discord_message:
                                            tools_used_after_text = True
                                        
                                        # Display tool use information
                                        tool_name = block.get("name", "unknown")
                                        tool_input = block.get("input", {})
                                        tool_id = block.get("id", "")[:8]  # Show first 8 chars of ID
                                        
                                        # Store tool info for later use in results
                                        current_tool_info = {
                                            'name': tool_name,
                                            'input': tool_input,
                                            'id': tool_id
                                        }
                                        
                                        # Format tool input nicely
                                        input_preview = ""
                                        if isinstance(tool_input, dict):
                                            # Show key details based on tool type
                                            if tool_name == "Bash" and "command" in tool_input:
                                                input_preview = f"Command: `{tool_input['command'][:100]}`"
                                            elif tool_name == "Read" and "file_path" in tool_input:
                                                # For Read, just show filename
                                                filename = tool_input['file_path'].split('/')[-1]
                                                input_preview = f"📄 `{filename}`"
                                            elif tool_name == "Write" and "file_path" in tool_input:
                                                input_preview = f"File: `{tool_input['file_path']}`"
                                            elif tool_name == "Edit" and "file_path" in tool_input:
                                                old_str = tool_input.get('old_string', '')[:50]
                                                input_preview = f"File: `{tool_input['file_path']}` (editing `{old_str}...`)"
                                            elif tool_name == "Task" and "prompt" in tool_input:
                                                # For Task, show full prompt without truncation
                                                input_preview = f"Prompt: {tool_input['prompt']}"
                                            elif tool_name in ["TodoRead", "TodoWrite"]:
                                                # For Todo tools, show brief description and actual content for TodoWrite
                                                if tool_name == "TodoRead":
                                                    input_preview = "📋 Reading todo list"
                                                else:
                                                    todos = tool_input.get('todos', [])
                                                    todos_count = len(todos)
                                                    input_preview = f"📋 Updating todo list ({todos_count} items)"
                                                    
                                                    # Also send the formatted todo list immediately for TodoWrite
                                                    if todos and ctx:
                                                        formatted_todos = format_todos_list(todos)
                                                        await ctx.send(formatted_todos)
                                                        activity_timeout.reset()  # Reset timeout
                                            elif tool_name == "MultiEdit" and "file_path" in tool_input:
                                                # For MultiEdit, show file and number of edits
                                                edits_count = len(tool_input.get('edits', []))
                                                input_preview = f"File: `{tool_input['file_path']}` ({edits_count} edits)"
                                            elif "path" in tool_input:
                                                input_preview = f"Path: `{tool_input['path']}`"
                                            else:
                                                # Show first few key-value pairs
                                                preview_items = []
                                                for k, v in list(tool_input.items())[:2]:
                                                    if isinstance(v, str) and len(v) > 50:
                                                        # Don't truncate Task prompts
                                                        if tool_name == "Task" and k == "prompt":
                                                            preview_items.append(f"{k}: {v}")
                                                        else:
                                                            v = v[:50] + "..."
                                                            preview_items.append(f"{k}: `{v}`")
                                                    else:
                                                        preview_items.append(f"{k}: `{v}`")
                                                input_preview = ", ".join(preview_items)
                                        
                                        # Special formatting for Read tool
                                        if tool_name == "Read":
                                            tool_msg = f"🔧 **Reading:** {input_preview}"
                                        else:
                                            tool_msg = f"🔧 **Tool Use:** {tool_name}"
                                            if input_preview:
                                                tool_msg += f"\n   {input_preview}"
                                        
                                        if ctx:
                                            await ctx.send(tool_msg)
                                            last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                        
                            elif msg_type == "user":
                                # User messages - show what was sent to Claude and tool results
                                user_msg = data.get('message', {})
                                message_content = ""
                                
                                if isinstance(user_msg.get('content'), list):
                                    for block in user_msg['content']:
                                        if block.get('type') == 'text':
                                            text = block.get('text', '')
                                            if text:
                                                message_content = f"**User:** {text}"
                                        elif block.get('type') == 'tool_result':
                                            # Handle tool results
                                            tool_use_id = block.get('tool_use_id', '')[:8]
                                            content = block.get('content', '')
                                            is_error = block.get('is_error', False)
                                            
                                            # Check if this is a read command result (very verbose)
                                            # Look for previous tool use to determine tool name
                                            is_read_result = False
                                            
                                            status = "❌" if is_error else "✅"
                                            result_preview = ""
                                            
                                            if content:
                                                # Check if content looks like a Read tool result (has line numbers)
                                                if '→' in content and any(line.strip().startswith(f'{i}→') for i in range(1, 20) for line in content.split('\n')[:20]):
                                                    is_read_result = True
                                                
                                                if is_read_result:
                                                    # For Read results, just show a summary
                                                    lines = content.split('\n')
                                                    line_count = len([l for l in lines if '→' in l])
                                                    # Try to extract filename from first few lines
                                                    filename = "file"
                                                    for line in lines[:5]:
                                                        if any(ext in line.lower() for ext in ['.py', '.js', '.ts', '.txt', '.md', '.json', '.yaml', '.yml', '.xml', '.html', '.css']):
                                                            # Extract potential filename
                                                            parts = line.split()
                                                            for part in parts:
                                                                if any(ext in part.lower() for ext in ['.py', '.js', '.ts', '.txt', '.md', '.json', '.yaml', '.yml', '.xml', '.html', '.css']):
                                                                    filename = part.split('/')[-1]  # Get just the filename
                                                                    break
                                                            break
                                                    result_preview = f"📄 Read {filename} ({line_count} lines)"
                                                else:
                                                    # Check if this is a todo result and format it nicely
                                                    is_todo_result = False
                                                    
                                                    # Multiple ways to detect todo content
                                                    if any(indicator in content.lower() for indicator in [
                                                        'todo list', 'status":"', 'priority":"', '"content":"',
                                                        'in_progress', 'pending', 'completed', 'remember to continue'
                                                    ]):
                                                        is_todo_result = True
                                                    
                                                    if is_todo_result:
                                                        result_preview = format_todo_content(content)
                                                    else:
                                                        # For other tool results, truncate if too long
                                                        if len(content) > 1000:
                                                            result_preview = content[:1000] + "\n... (truncated)"
                                                        else:
                                                            result_preview = content
                                                        
                                                        # Format as code block if it looks like output
                                                        if '\n' in result_preview or any(c in result_preview for c in ['/', '\\', '$', '>']):
                                                            result_preview = f"```\n{result_preview}\n```"
                                                        else:
                                                            result_preview = f"`{result_preview}`"
                                            
                                            tool_result_msg = f"{status} **Tool Result**"
                                            if result_preview:
                                                tool_result_msg += f"\n{result_preview}"
                                            
                                            if ctx:
                                                if is_read_result:
                                                    # Don't send read results at all - they're handled in the summary above
                                                    pass
                                                elif is_todo_result:
                                                    # Always send todo results, they're important for user visibility
                                                    await send_long_message(ctx, tool_result_msg)
                                                    last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                                else:
                                                    # Send other tool results with full content (but truncated)
                                                    await send_long_message(ctx, tool_result_msg)
                                                    last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                
                                elif isinstance(user_msg.get('content'), str):
                                    message_content = f"**User:** {user_msg['content']}"
                                
                                if message_content and ctx:
                                    await ctx.send(message_content)
                                    last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                
                            elif msg_type == "system":
                                # System messages - show progress info
                                subtype = data.get("subtype", "")
                                message_content = ""
                                if subtype == "thinking":
                                    message_content = "🤔 *Claude is thinking...*"
                                elif subtype == "tool_use":
                                    tool_name = data.get("tool_name", "unknown")
                                    message_content = f"🔧 *Using tool: {tool_name}*"
                                elif subtype in ["tool_result", "tool_error"]:
                                    tool_name = data.get("tool_name", "unknown")
                                    status = "✅" if subtype == "tool_result" else "❌"
                                    
                                    # For Read tool results, show line count
                                    if tool_name == "Read" and subtype == "tool_result":
                                        # Try to get result content from the data
                                        result_content = data.get("content", "")
                                        if result_content and '→' in result_content:
                                            lines = result_content.split('\n')
                                            line_count = len([l for l in lines if '→' in l])
                                            message_content = f"{status} *Read completed ({line_count} lines)*"
                                        else:
                                            message_content = f"{status} *Read completed*"
                                    else:
                                        message_content = f"{status} *Tool {tool_name} completed*"
                                
                                if message_content and ctx:
                                    await ctx.send(message_content)
                                    last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                    
                            elif msg_type == "result":
                                # Result message - show completion and final message update
                                num_turns = data.get('num_turns', 0)
                                is_error = data.get('is_error', False)
                                result_content = data.get('result', '')
                                
                                # Check for usage limit error
                                if is_error and "Claude AI usage limit reached|" in result_content:
                                    formatted_error = format_usage_limit_message(result_content)
                                    if ctx:
                                        await ctx.send(formatted_error)
                                        last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                    logger.info(f"Usage limit reached: {result_content}")
                                    return  # Don't process further
                                
                                # Send any remaining content that hasn't been sent yet
                                if current_assistant_message and ctx:
                                    if sent_text_length < len(current_assistant_message):
                                        # There's new content to add
                                        remaining_content = current_assistant_message[sent_text_length:]
                                        if remaining_content.strip():
                                            try:
                                                await send_long_message(ctx, remaining_content)
                                                sent_text_length = len(current_assistant_message)
                                                last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                            except Exception as e:
                                                logger.error(f"Error sending final message: {e}")
                                
                                message_content = f"✨ *Conversation completed ({num_turns} turns)*"
                                if ctx:
                                    await ctx.send(message_content)
                                    last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                logger.info(f"Got result message with {num_turns} turns")
                            
                        except json.JSONDecodeError:
                            # Non-JSON lines, might be progress info or partial JSON
                            claude_stream_logger.info(f"NON_JSON_LINE: {repr(line)}")
                            logger.debug(f"Non-JSON line: {line[:100]}...")
                            continue
                        
                except Exception as e:
                    if "transport endpoint is not connected" in str(e).lower():
                        # Process ended normally
                        if current_assistant_message and ctx:
                            if last_discord_message and not tools_used_after_text:
                                # Send remaining text that hasn't been sent yet
                                remaining_text = current_assistant_message[sent_text_length:]
                                if remaining_text.strip():
                                    await send_long_message(ctx, remaining_text)
                                    sent_text_length = len(current_assistant_message)
                                    last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                            else:
                                # Send only remaining text if tools were used
                                if tools_used_after_text and sent_text_length < len(current_assistant_message):
                                    remaining_text = current_assistant_message[sent_text_length:]
                                    if remaining_text.strip():
                                        await send_long_message(ctx, remaining_text)
                                        sent_text_length = len(current_assistant_message)  # Update tracking
                                        last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                elif not tools_used_after_text and sent_text_length == 0:
                                    # Send full message only if nothing has been sent yet
                                    await send_long_message(ctx, current_assistant_message)
                                    sent_text_length = len(current_assistant_message)  # Update tracking
                                    last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                                elif not tools_used_after_text and sent_text_length < len(current_assistant_message):
                                    # Send only remaining text if some was already sent
                                    remaining_text = current_assistant_message[sent_text_length:]
                                    if remaining_text.strip():
                                        await send_long_message(ctx, remaining_text)
                                        sent_text_length = len(current_assistant_message)  # Update tracking
                                        last_activity_time = asyncio.get_event_loop().time()  # Reset timeout
                        break
                    logger.error(f"Error reading stdout: {e}")
                    break
        
        async def read_stderr():
            """Read stderr stream with timeout"""
            while True:
                try:
                    if process.stderr is None:
                        break
                    
                    try:
                        line = await asyncio.wait_for(process.stderr.readline(), timeout=5.0)
                    except asyncio.TimeoutError:
                        # No error data for 5 seconds, check if process is still alive
                        if process.returncode is not None:
                            break
                        continue
                    except Exception as e:
                        logger.error(f"Error reading stderr line: {e}")
                        break
                    
                    if not line:
                        break
                    stderr_text = line.decode().strip()
                    
                    # Log raw stderr
                    claude_stream_logger.info(f"STDERR: {repr(stderr_text)}")
                    
                    error_parts.append(stderr_text)
                except Exception as e:
                    logger.error(f"Error reading stderr: {e}")
                    break
        
        # Run both readers with dynamic timeout that resets on activity
        async def run_with_activity_timeout():
            readers_task = asyncio.create_task(asyncio.gather(read_stdout(), read_stderr()))
            
            while not readers_task.done():
                try:
                    remaining_time = activity_timeout.time_remaining()
                    if remaining_time <= 0:
                        logger.error("Claude CLI process timed out due to inactivity (5 minutes)")
                        readers_task.cancel()
                        try:
                            process.terminate()
                            await asyncio.wait_for(process.wait(), timeout=5.0)
                        except:
                            process.kill()
                        return "Error: Claude CLI process timed out due to inactivity"
                    
                    # Wait for completion or short timeout to check activity again
                    await asyncio.wait_for(asyncio.shield(readers_task), timeout=min(remaining_time, 10.0))
                    break
                    
                except asyncio.TimeoutError:
                    # Check if process finished
                    if process.returncode is not None:
                        break
                    # Continue loop to check activity timeout again
                    continue
                    
            return await readers_task
        
        try:
            await run_with_activity_timeout()
        except Exception as e:
            if "timed out" in str(e):
                return str(e)
            logger.error(f"Error in activity timeout handler: {e}")
            return f"Error: {e}"
        
        # Wait for process to complete with timeout
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Process did not terminate gracefully")
            try:
                process.kill()
                await process.wait()
            except:
                pass
        
        if process.returncode != 0:
            error_msg = '\n'.join(error_parts) if error_parts else "Unknown error"
            logger.error(f"Claude CLI error (code {process.returncode}): {error_msg}")
            # Clear global process reference
            current_claude_process = None
            current_claude_channel = None
            return f"Error: {error_msg}"
        
        # Clear global process reference on successful completion
        current_claude_process = None
        current_claude_channel = None
        
        result = ''.join(response_parts).strip()
        return result if result else "No response generated"
        
    except Exception as e:
        logger.error(f"Error calling enhanced Claude CLI: {e}")
        # Clear global process reference on exception
        current_claude_process = None
        current_claude_channel = None
        return f"Error: {str(e)}"

async def call_claude_cli(prompt: str, system_prompt: str = None, tools: list = None, max_turns: int = 1) -> str:
    """Call Claude CLI directly as fallback"""
    try:
        cmd = [
            CLAUDE_CLI_PATH,
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(max_turns),
            "--print", prompt
        ]
        
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
            
        if tools:
            cmd.extend(["--allowedTools", ",".join(tools)])
        
        # Run subprocess
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"Claude CLI error: {stderr.decode()}")
            return f"Error: {stderr.decode()}"
        
        # Parse JSON output
        response_parts = []
        for line in stdout.decode().strip().split('\n'):
            if line.strip():
                try:
                    data = json.loads(line)
                    if data.get("type") == "assistant" and "message" in data:
                        for block in data["message"].get("content", []):
                            if block.get("type") == "text":
                                response_parts.append(block.get("text", ""))
                except json.JSONDecodeError:
                    continue
        
        return ''.join(response_parts).strip()
        
    except Exception as e:
        logger.error(f"Error calling Claude CLI: {e}")
        return f"Error: {str(e)}"

@bot.command(name='claude')
async def claude_query(ctx, *, prompt: str = ""):
    """Query Claude with persistent conversation and all tools enabled"""
    try:
        # Process any file attachments
        full_prompt = prompt
        if ctx.message.attachments:
            await ctx.send("📎 Processing attachments...")
            attachment_contents = []
            
            for attachment in ctx.message.attachments:
                content = await download_and_read_attachment(attachment)
                attachment_contents.append(content)
            
            # Add attachment contents to the prompt
            if attachment_contents:
                attachments_text = "\n\n".join(attachment_contents)
                if prompt:
                    full_prompt = f"{prompt}\n\n{attachments_text}"
                else:
                    full_prompt = f"Please analyze these uploaded files:\n\n{attachments_text}"
        
        if not full_prompt.strip():
            await ctx.send("Please provide a prompt or upload files to analyze.")
            return
        
        # Log the start of a new Claude interaction
        claude_stream_logger.info(f"=== NEW CLAUDE INTERACTION ===")
        claude_stream_logger.info(f"USER: {ctx.author} | CHANNEL: {ctx.channel} | PROMPT: {repr(full_prompt[:200])}")
        
        await ctx.send("🤔 Thinking...")
        
        response = await call_claude_enhanced(
            prompt=full_prompt,
            system_prompt="You are a helpful Discord bot assistant. Keep responses concise and Discord-friendly.",
            tools=["Read", "Write", "Edit", "MultiEdit", "LS", "NotebookRead", "NotebookEdit", 
                   "Glob", "Grep", "Task", "Bash", "WebFetch", "WebSearch", "TodoRead", "TodoWrite", "exit_plan_mode"],
            continue_conversation=True,
            ctx=ctx
        )
        
        # Log the end of interaction
        claude_stream_logger.info(f"=== END CLAUDE INTERACTION ===")
        
        # Response handling is now done in real-time streaming
        # Only send final response if there was an error
        if response and response.startswith("Error:"):
            await ctx.send(response)
            
    except Exception as e:
        logger.error(f"Error querying Claude: {e}")
        claude_stream_logger.info(f"ERROR: {repr(str(e))}")
        await ctx.send(f"Sorry, I encountered an error: {str(e)}")


@bot.command(name='claude_new')
async def claude_new_query(ctx, *, prompt: str = ""):
    """Start a new Claude conversation (fresh session)"""
    try:
        # Process any file attachments
        full_prompt = prompt
        if ctx.message.attachments:
            await ctx.send("📎 Processing attachments...")
            attachment_contents = []
            
            for attachment in ctx.message.attachments:
                content = await download_and_read_attachment(attachment)
                attachment_contents.append(content)
            
            # Add attachment contents to the prompt
            if attachment_contents:
                attachments_text = "\n\n".join(attachment_contents)
                if prompt:
                    full_prompt = f"{prompt}\n\n{attachments_text}"
                else:
                    full_prompt = f"Please analyze these uploaded files:\n\n{attachments_text}"
        
        if not full_prompt.strip():
            await ctx.send("Please provide a prompt or upload files to analyze.")
            return
        
        # Log the start of a new Claude interaction
        claude_stream_logger.info(f"=== NEW CLAUDE CONVERSATION (FRESH) ===")
        claude_stream_logger.info(f"USER: {ctx.author} | CHANNEL: {ctx.channel} | PROMPT: {repr(full_prompt[:200])}")
        
        await ctx.send("🆕 Starting new conversation...")
        
        response = await call_claude_enhanced(
            prompt=full_prompt,
            system_prompt="You are a helpful Discord bot assistant. Keep responses concise and Discord-friendly.",
            tools=["Read", "Write", "Edit", "MultiEdit", "LS", "NotebookRead", "NotebookEdit", 
                   "Glob", "Grep", "Task", "Bash", "WebFetch", "WebSearch", "TodoRead", "TodoWrite", "exit_plan_mode"],
            ctx=ctx
        )
        
        # Log the end of interaction
        claude_stream_logger.info(f"=== END CLAUDE CONVERSATION ===")
        
        # Response handling is now done in real-time streaming
        # Only send final response if there was an error
        if response and response.startswith("Error:"):
            await ctx.send(response)
            
    except Exception as e:
        logger.error(f"Error starting new Claude conversation: {e}")
        claude_stream_logger.info(f"ERROR: {repr(str(e))}")
        await ctx.send(f"Sorry, I encountered an error: {str(e)}")

@bot.command(name='claude_resume')
async def claude_resume_query(ctx, session_id: str, *, prompt: str = ""):
    """Resume a specific Claude conversation by session ID"""
    try:
        # Process any file attachments
        full_prompt = prompt
        if ctx.message.attachments:
            await ctx.send("📎 Processing attachments...")
            attachment_contents = []
            
            for attachment in ctx.message.attachments:
                content = await download_and_read_attachment(attachment)
                attachment_contents.append(content)
            
            # Add attachment contents to the prompt
            if attachment_contents:
                attachments_text = "\n\n".join(attachment_contents)
                if prompt:
                    full_prompt = f"{prompt}\n\n{attachments_text}"
                else:
                    full_prompt = f"Please analyze these uploaded files:\n\n{attachments_text}"
        
        if not full_prompt.strip():
            await ctx.send("Please provide a prompt or upload files to analyze.")
            return
        
        # Log the start of a resumed Claude interaction
        claude_stream_logger.info(f"=== RESUME CLAUDE CONVERSATION ===")
        claude_stream_logger.info(f"USER: {ctx.author} | CHANNEL: {ctx.channel} | SESSION: {session_id} | PROMPT: {repr(full_prompt[:200])}")
        
        await ctx.send(f"🔄 Resuming session {session_id[:8]}...")
        
        response = await call_claude_enhanced(
            prompt=full_prompt,
            system_prompt="You are a helpful Discord bot assistant. Keep responses concise and Discord-friendly.",
            tools=["Read", "Write", "Edit", "MultiEdit", "LS", "NotebookRead", "NotebookEdit", 
                   "Glob", "Grep", "Task", "Bash", "WebFetch", "WebSearch", "TodoRead", "TodoWrite", "exit_plan_mode"],
            resume_session=session_id,
            ctx=ctx
        )
        
        # Log the end of interaction
        claude_stream_logger.info(f"=== END CLAUDE RESUME ===")
        
        # Response handling is now done in real-time streaming
        # Only send final response if there was an error
        if response and response.startswith("Error:"):
            await ctx.send(response)
            
    except Exception as e:
        logger.error(f"Error resuming Claude conversation: {e}")
        claude_stream_logger.info(f"ERROR: {repr(str(e))}")
        await ctx.send(f"Sorry, I encountered an error: {str(e)}")

@bot.command(name='stop')
async def stop_claude(ctx):
    """Stop the currently running Claude subprocess gracefully"""
    global current_claude_process, current_claude_channel
    
    if current_claude_process is None:
        await ctx.send("🚫 No Claude process is currently running.")
        return
    
    try:
        # Check if process is still running
        if current_claude_process.returncode is not None:
            await ctx.send("ℹ️ Claude process has already completed.")
            current_claude_process = None
            current_claude_channel = None
            return
        
        await ctx.send("🛑 Stopping Claude process gracefully...")
        
        # Try graceful termination first
        current_claude_process.terminate()
        logger.info("Sent SIGTERM to Claude process")
        
        try:
            # Wait up to 10 seconds for graceful shutdown
            await asyncio.wait_for(current_claude_process.wait(), timeout=10.0)
            logger.info("Claude process terminated gracefully")
            termination_method = "gracefully terminated"
        except asyncio.TimeoutError:
            # Force kill if it doesn't terminate gracefully
            logger.warning("Claude process did not terminate gracefully, forcing kill")
            current_claude_process.kill()
            await current_claude_process.wait()
            termination_method = "force killed"
        
        # Get the return code
        return_code = current_claude_process.returncode
        
        # Store original channel before clearing
        original_channel = current_claude_channel
        
        # Clear global references
        current_claude_process = None
        current_claude_channel = None
        
        # Send results to Discord
        result_message = f"✅ **Claude Process Stopped**\n"
        result_message += f"📊 **Details:**\n"
        result_message += f"• Status: {termination_method}\n"
        result_message += f"• Exit code: {return_code}\n"
        result_message += f"• Stopped by: {ctx.author.mention}"
        
        await ctx.send(result_message)
        
        # Also send to the original channel if different
        if original_channel and original_channel != ctx.channel:
            await original_channel.send(f"🛑 Claude process was stopped by {ctx.author.mention} in {ctx.channel.mention}")
        
        logger.info(f"Claude process stopped by {ctx.author} (ID: {ctx.author.id}) in channel {ctx.channel}")
        
    except Exception as e:
        logger.error(f"Error stopping Claude process: {e}")
        await ctx.send(f"❌ Error stopping Claude process: {str(e)}")
        # Clear references even on error
        current_claude_process = None
        current_claude_channel = None

@bot.command(name='help_claude')
async def help_claude(ctx):
    """Show available Claude bot commands"""
    help_text = """
**Claude Bot Commands:**
• `!claude <prompt>` - Ask Claude (continues previous conversation)
• `!claude_new <prompt>` - Start a fresh conversation
• `!claude_resume <session_id> <prompt>` - Resume a specific conversation
• `!stop` - Stop the currently running Claude process gracefully
• `!help_claude` - Show this help message

**File Upload Support:**
• Attach files to any !claude command to have Claude analyze them
• Works with text files (code, documents, logs, etc.)
• Binary files show file info but content can't be displayed as text
• You can upload files with or without a text prompt

**Examples:**
• `!claude What is Python?`
• `!claude Can you elaborate on that?` (continues from previous)
• `!claude_new Tell me about JavaScript` (fresh start)
• `!claude_resume abc123 What did we discuss earlier?`
• `!claude Analyze this code` (with attached .py file)
• `!claude` (with just attached files, no text prompt)

**Features:**
• All tools enabled (Read, Write, Edit, WebSearch, Bash, etc.)
• Persistent conversations by default
• No turn limits - conversations can go as long as needed
• Web search and file operations available
• File upload and analysis support
    """
    await ctx.send(help_text)

async def main():
    discord_token = os.getenv('DISCORD_BOT_TOKEN')
    if not discord_token:
        logger.error("DISCORD_BOT_TOKEN environment variable not set")
        return
        
    try:
        await bot.start(discord_token)
    except KeyboardInterrupt:
        logger.info("Bot shutdown requested")
    except Exception as e:
        logger.error(f"Bot error: {e}")
    finally:
        await bot.close()

if __name__ == '__main__':
    asyncio.run(main())
