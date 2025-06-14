import os
import asyncio
import logging
import json
import subprocess
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv
from claude_code_sdk import query, ClaudeCodeOptions

load_dotenv()

# Set Claude CLI path in environment
os.environ["PATH"] = f"/home/andrew/.claude/local:{os.environ.get('PATH', '')}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CLAUDE_CLI_PATH = "/home/andrew/.claude/local/claude"

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

async def call_claude_enhanced(prompt: str, system_prompt: str = None, tools: list = None, 
                             continue_conversation: bool = False, resume_session: str = None) -> str:
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
            """Read and parse stdout stream continuously"""
            while True:
                try:
                    if process.stdout is None:
                        break
                    line = await process.stdout.readline()
                    if not line:
                        # Stream ended - check if process is still running
                        if process.returncode is not None:
                            # Process has terminated
                            break
                        # Process still running, wait a bit and continue
                        await asyncio.sleep(0.1)
                        continue
                    
                    line = line.decode().strip()
                    if not line:
                        continue
                        
                    try:
                        data = json.loads(line)
                        msg_type = data.get("type")
                        
                        if msg_type == "assistant" and "message" in data:
                            # Handle assistant messages - collect all text blocks
                            for block in data["message"].get("content", []):
                                if block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text:
                                        response_parts.append(text)
                                        logger.debug(f"Added assistant text: {text[:50]}...")
                                        
                        elif msg_type == "user":
                            # User messages - usually just echoing the prompt
                            logger.debug(f"User message: {data.get('message', {}).get('content', '')[:50]}...")
                            continue
                            
                        elif msg_type == "system":
                            # System messages - might contain progress info
                            subtype = data.get("subtype", "")
                            logger.debug(f"System message: {subtype}")
                            if subtype == "thinking":
                                # Claude is thinking
                                continue
                            elif subtype == "tool_use":
                                # Tool is being used
                                continue
                                
                        elif msg_type == "result":
                            # Result message - log but keep reading
                            logger.info(f"Got result message with {data.get('num_turns', 0)} turns")
                            continue
                            
                    except json.JSONDecodeError:
                        # Non-JSON lines, might be progress info or partial JSON
                        logger.debug(f"Non-JSON line: {line[:100]}...")
                        continue
                        
                except Exception as e:
                    if "transport endpoint is not connected" in str(e).lower():
                        # Process ended normally
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
            continue_conversation=True
        )
        
        if len(response) > 2000:
            response = response[:1997] + "..."
            
        if response and not response.startswith("Error:"):
            await ctx.send(response)
        else:
            await ctx.send(response if response.startswith("Error:") else "Sorry, I couldn't generate a response.")
            
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
                   "Glob", "Grep", "Task", "Bash", "WebFetch", "WebSearch", "TodoRead", "TodoWrite", "exit_plan_mode"]
        )
        
        if len(response) > 2000:
            response = response[:1997] + "..."
            
        if response and not response.startswith("Error:"):
            await ctx.send(response)
        else:
            await ctx.send(response if response.startswith("Error:") else "Sorry, I couldn't generate a response.")
            
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
            resume_session=session_id
        )
        
        if len(response) > 2000:
            response = response[:1997] + "..."
            
        if response and not response.startswith("Error:"):
            await ctx.send(response)
        else:
            await ctx.send(response if response.startswith("Error:") else "Sorry, I couldn't generate a response.")
            
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