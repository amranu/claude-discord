import os
import asyncio
import logging
import logging.handlers
import json
import subprocess
from typing import Optional
from datetime import datetime

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

async def call_claude_enhanced(prompt: str, system_prompt: str = None, tools: list = None, 
                             continue_conversation: bool = False, resume_session: str = None, ctx=None) -> str:
    """Enhanced Claude CLI call that handles streaming responses properly"""
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
        
        async def read_stdout():
            """Read and parse stdout stream continuously with true streaming"""
            last_send_time = asyncio.get_event_loop().time()
            accumulated_text = ""
            current_assistant_message = ""
            last_discord_message = None
            partial_line = ""
            
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
                                if last_discord_message:
                                    try:
                                        await last_discord_message.edit(content=current_assistant_message[:2000])
                                    except:
                                        await send_long_message(ctx, current_assistant_message)
                                else:
                                    await send_long_message(ctx, current_assistant_message)
                            elif accumulated_text and ctx:
                                await send_long_message(ctx, accumulated_text)
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
                                                        if last_discord_message:
                                                            await last_discord_message.edit(content=message_to_send)
                                                        else:
                                                            last_discord_message = await ctx.send(message_to_send)
                                                    except discord.errors.HTTPException:
                                                        # If edit fails, send new message
                                                        last_discord_message = await ctx.send(message_to_send)
                                                    except Exception as e:
                                                        logger.error(f"Error updating Discord message: {e}")
                                                    
                                                    last_send_time = current_time
                                    
                                    elif block.get("type") == "tool_use":
                                        # Display tool use information
                                        tool_name = block.get("name", "unknown")
                                        tool_input = block.get("input", {})
                                        tool_id = block.get("id", "")[:8]  # Show first 8 chars of ID
                                        
                                        # Format tool input nicely
                                        input_preview = ""
                                        if isinstance(tool_input, dict):
                                            # Show key details based on tool type
                                            if tool_name == "Bash" and "command" in tool_input:
                                                input_preview = f"Command: `{tool_input['command'][:100]}`"
                                            elif tool_name in ["Read", "Write"] and "file_path" in tool_input:
                                                input_preview = f"File: `{tool_input['file_path']}`"
                                            elif tool_name == "Edit" and "file_path" in tool_input:
                                                old_str = tool_input.get('old_string', '')[:50]
                                                input_preview = f"File: `{tool_input['file_path']}` (editing `{old_str}...`)"
                                            elif "path" in tool_input:
                                                input_preview = f"Path: `{tool_input['path']}`"
                                            else:
                                                # Show first few key-value pairs
                                                preview_items = []
                                                for k, v in list(tool_input.items())[:2]:
                                                    if isinstance(v, str) and len(v) > 50:
                                                        v = v[:50] + "..."
                                                    preview_items.append(f"{k}: `{v}`")
                                                input_preview = ", ".join(preview_items)
                                        
                                        tool_msg = f"🔧 **Tool Use:** {tool_name}"
                                        if input_preview:
                                            tool_msg += f"\n   {input_preview}"
                                        
                                        if ctx:
                                            await ctx.send(tool_msg)
                                        
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
                                            
                                            status = "❌" if is_error else "✅"
                                            result_preview = ""
                                            
                                            if content:
                                                # Don't truncate - show full output but split long messages
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
                                                await send_long_message(ctx, tool_result_msg)
                                
                                elif isinstance(user_msg.get('content'), str):
                                    message_content = f"**User:** {user_msg['content']}"
                                
                                if message_content and ctx:
                                    await ctx.send(message_content)
                                
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
                                    message_content = f"{status} *Tool {tool_name} completed*"
                                
                                if message_content and ctx:
                                    await ctx.send(message_content)
                                    
                            elif msg_type == "result":
                                # Result message - show completion and final message update
                                num_turns = data.get('num_turns', 0)
                                
                                # Make final update to the last Discord message if there's any remaining content
                                if current_assistant_message and ctx:
                                    if last_discord_message:
                                        try:
                                            # Send the complete final message
                                            if len(current_assistant_message) <= 2000:
                                                await last_discord_message.edit(content=current_assistant_message)
                                            else:
                                                # If too long, edit with truncated version and send full version
                                                await last_discord_message.edit(content=current_assistant_message[:1997] + "...")
                                                await send_long_message(ctx, current_assistant_message)
                                        except Exception as e:
                                            logger.error(f"Error updating final message: {e}")
                                            await send_long_message(ctx, current_assistant_message)
                                    else:
                                        await send_long_message(ctx, current_assistant_message)
                                
                                message_content = f"✨ *Conversation completed ({num_turns} turns)*"
                                if ctx:
                                    await ctx.send(message_content)
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
                            if last_discord_message:
                                try:
                                    await last_discord_message.edit(content=current_assistant_message[:2000])
                                except:
                                    await send_long_message(ctx, current_assistant_message)
                            else:
                                await send_long_message(ctx, current_assistant_message)
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
        
        # Run both readers concurrently with timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(read_stdout(), read_stderr()), 
                timeout=300.0  # 5 minute timeout for long operations
            )
        except asyncio.TimeoutError:
            logger.error("Claude CLI process timed out after 5 minutes")
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except:
                process.kill()
            return "Error: Claude CLI process timed out"
        
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
            return f"Error: {error_msg}"
        
        result = ''.join(response_parts).strip()
        return result if result else "No response generated"
        
    except Exception as e:
        logger.error(f"Error calling enhanced Claude CLI: {e}")
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
async def claude_query(ctx, *, prompt: str):
    """Query Claude with persistent conversation and all tools enabled"""
    try:
        # Log the start of a new Claude interaction
        claude_stream_logger.info(f"=== NEW CLAUDE INTERACTION ===")
        claude_stream_logger.info(f"USER: {ctx.author} | CHANNEL: {ctx.channel} | PROMPT: {repr(prompt)}")
        
        await ctx.send("🤔 Thinking...")
        
        response = await call_claude_enhanced(
            prompt=prompt,
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
async def claude_new_query(ctx, *, prompt: str):
    """Start a new Claude conversation (fresh session)"""
    try:
        # Log the start of a new Claude interaction
        claude_stream_logger.info(f"=== NEW CLAUDE CONVERSATION (FRESH) ===")
        claude_stream_logger.info(f"USER: {ctx.author} | CHANNEL: {ctx.channel} | PROMPT: {repr(prompt)}")
        
        await ctx.send("🆕 Starting new conversation...")
        
        response = await call_claude_enhanced(
            prompt=prompt,
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
async def claude_resume_query(ctx, session_id: str, *, prompt: str):
    """Resume a specific Claude conversation by session ID"""
    try:
        # Log the start of a resumed Claude interaction
        claude_stream_logger.info(f"=== RESUME CLAUDE CONVERSATION ===")
        claude_stream_logger.info(f"USER: {ctx.author} | CHANNEL: {ctx.channel} | SESSION: {session_id} | PROMPT: {repr(prompt)}")
        
        await ctx.send(f"🔄 Resuming session {session_id[:8]}...")
        
        response = await call_claude_enhanced(
            prompt=prompt,
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

@bot.command(name='help_claude')
async def help_claude(ctx):
    """Show available Claude bot commands"""
    help_text = """
**Claude Bot Commands:**
• `!claude <prompt>` - Ask Claude (continues previous conversation)
• `!claude_new <prompt>` - Start a fresh conversation
• `!claude_resume <session_id> <prompt>` - Resume a specific conversation
• `!help_claude` - Show this help message

**Examples:**
• `!claude What is Python?`
• `!claude Can you elaborate on that?` (continues from previous)
• `!claude_new Tell me about JavaScript` (fresh start)
• `!claude_resume abc123 What did we discuss earlier?`

**Features:**
• All tools enabled (Read, Write, Edit, WebSearch, Bash, etc.)
• Persistent conversations by default
• No turn limits - conversations can go as long as needed
• Web search and file operations available
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
