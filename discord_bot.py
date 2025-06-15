import os
import asyncio
import logging
import json
import subprocess
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv
#from claude_code_sdk import query, ClaudeCodeOptions

load_dotenv()

# Set Claude CLI path in environment
os.environ["PATH"] = f"/usr/local/bin/claude:{os.environ.get('PATH', '')}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    
    continuation_prefix = "*(...continued)*\n"
    
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
    
    # Send all chunks, accounting for continuation prefix length
    for i, chunk in enumerate(chunks):
        if i == 0:
            await ctx.send(chunk)
        else:
            # Make sure continuation message fits within limit
            continuation_msg = continuation_prefix + chunk
            if len(continuation_msg) <= max_length:
                await ctx.send(continuation_msg)
            else:
                # Split the chunk further to account for prefix
                available_length = max_length - len(continuation_prefix)
                sub_chunks = split_text(chunk, available_length)
                for j, sub_chunk in enumerate(sub_chunks):
                    await ctx.send(continuation_prefix + sub_chunk)

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
        
        # Run subprocess with streaming
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
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
                    
                    # Read smaller chunks for true streaming
                    chunk = await process.stdout.read(1024)
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
                    partial_line += chunk_text
                    
                    # Process complete lines
                    while '\n' in partial_line:
                        line, partial_line = partial_line.split('\n', 1)
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
                                        
                            elif msg_type == "user":
                                # User messages - show what was sent to Claude
                                user_msg = data.get('message', {})
                                message_content = ""
                                if isinstance(user_msg.get('content'), list):
                                    for block in user_msg['content']:
                                        if block.get('type') == 'text':
                                            text = block.get('text', '')
                                            if text:
                                                message_content = f"**User:** {text}"
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
                                # Result message - show completion
                                num_turns = data.get('num_turns', 0)
                                message_content = f"✨ *Conversation completed ({num_turns} turns)*"
                                if ctx:
                                    await ctx.send(message_content)
                                logger.info(f"Got result message with {num_turns} turns")
                            
                        except json.JSONDecodeError:
                            # Non-JSON lines, might be progress info or partial JSON
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
            """Read stderr stream"""
            while True:
                try:
                    if process.stderr is None:
                        break
                    line = await process.stderr.readline()
                    if not line:
                        break
                    error_parts.append(line.decode().strip())
                except Exception as e:
                    logger.error(f"Error reading stderr: {e}")
                    break
        
        # Run both readers concurrently
        await asyncio.gather(read_stdout(), read_stderr())
        
        # Wait for process to complete
        await process.wait()
        
        if process.returncode != 0:
            error_msg = '\n'.join(error_parts)
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
        await ctx.send("🤔 Thinking...")
        
        response = await call_claude_enhanced(
            prompt=prompt,
            system_prompt="You are a helpful Discord bot assistant. Keep responses concise and Discord-friendly.",
            tools=["Read", "Write", "Edit", "MultiEdit", "LS", "NotebookRead", "NotebookEdit", 
                   "Glob", "Grep", "Task", "Bash", "WebFetch", "WebSearch", "TodoRead", "TodoWrite", "exit_plan_mode"],
            continue_conversation=True,
            ctx=ctx
        )
        
        # Response handling is now done in real-time streaming
        # Only send final response if there was an error
        if response and response.startswith("Error:"):
            await ctx.send(response)
            
    except Exception as e:
        logger.error(f"Error querying Claude: {e}")
        await ctx.send(f"Sorry, I encountered an error: {str(e)}")


@bot.command(name='claude_new')
async def claude_new_query(ctx, *, prompt: str):
    """Start a new Claude conversation (fresh session)"""
    try:
        await ctx.send("🆕 Starting new conversation...")
        
        response = await call_claude_enhanced(
            prompt=prompt,
            system_prompt="You are a helpful Discord bot assistant. Keep responses concise and Discord-friendly.",
            tools=["Read", "Write", "Edit", "MultiEdit", "LS", "NotebookRead", "NotebookEdit", 
                   "Glob", "Grep", "Task", "Bash", "WebFetch", "WebSearch", "TodoRead", "TodoWrite", "exit_plan_mode"],
            ctx=ctx
        )
        
        # Response handling is now done in real-time streaming
        # Only send final response if there was an error
        if response and response.startswith("Error:"):
            await ctx.send(response)
            
    except Exception as e:
        logger.error(f"Error starting new Claude conversation: {e}")
        await ctx.send(f"Sorry, I encountered an error: {str(e)}")

@bot.command(name='claude_resume')
async def claude_resume_query(ctx, session_id: str, *, prompt: str):
    """Resume a specific Claude conversation by session ID"""
    try:
        await ctx.send(f"🔄 Resuming session {session_id[:8]}...")
        
        response = await call_claude_enhanced(
            prompt=prompt,
            system_prompt="You are a helpful Discord bot assistant. Keep responses concise and Discord-friendly.",
            tools=["Read", "Write", "Edit", "MultiEdit", "LS", "NotebookRead", "NotebookEdit", 
                   "Glob", "Grep", "Task", "Bash", "WebFetch", "WebSearch", "TodoRead", "TodoWrite", "exit_plan_mode"],
            resume_session=session_id,
            ctx=ctx
        )
        
        # Response handling is now done in real-time streaming
        # Only send final response if there was an error
        if response and response.startswith("Error:"):
            await ctx.send(response)
            
    except Exception as e:
        logger.error(f"Error resuming Claude conversation: {e}")
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
