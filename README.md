# Claude Discord Bot

A Discord bot powered by the Claude Code SDK that allows users to interact with Claude directly from Discord.

## Features

- **Basic Claude queries** - Ask Claude questions using `!claude`
- **Code analysis** - Use Claude with code tools via `!claude_code`
- **Automatic response chunking** - Handles Discord's 2000 character limit
- **Error handling** - Graceful error messages for users

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Install Claude Code CLI:**
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

3. **Create Discord Bot:**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - Create a new application and bot
   - Copy the bot token

4. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env and add your Discord bot token
   ```

5. **Invite bot to server:**
   - In Discord Developer Portal, go to OAuth2 > URL Generator
   - Select "bot" scope and required permissions
   - Use generated URL to invite bot

## Usage

### Commands

- `!claude <prompt>` - Ask Claude a question
- `!claude_code <prompt>` - Ask Claude with code analysis tools
- `!help_claude` - Show available commands

### Examples

```
!claude What is Python?
!claude Explain async/await in Python
!claude_code Analyze the code structure in this repository
```

## Running

```bash
python discord_bot.py
```

## Requirements

- Python 3.10+
- Node.js (for Claude Code CLI)
- Discord bot token
- Claude Code CLI installed globally