# Telegram Private Channel File Extractor

This bot allows you to copy media and messages from a private channel (that you have joined) to another channel of your choice. It copies the messages (re-uploads them) so they are preserved even if the source is deleted.

## setup

1.  **Dependencies**: (Already installed)
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configuration**:
    Open the `.env` file in this folder and fill in the details:
    *   `API_ID` & `API_HASH`: Get these from [my.telegram.org](https://my.telegram.org).
    *   `BOT_TOKEN`: Create a new bot via [@BotFather](https://t.me/BotFather) and get the token.
    *   `OWNER_ID`: Your Telegram User ID (get it from [@userinfobot](https://t.me/userinfobot)).
    *   `SESSION_STRING`: Run the session generator script below to get this.

3.  **Generate Session String** (Crucial Step):
    Run this command and follow the login instructions in the terminal:
    ```bash
    python generate_session.py
    ```
    Copy the long string it outputs and paste it into `SESSION_STRING` in the `.env` file.

## Usage

1.  **Start the Bot**:
    ```bash
    python main.py
    ```

2.  **Go to your Bot in Telegram** (the one you made in BotFather).

3.  **Send the command**:
    ```
    /copy <Private_Link> <Destination_Channel_ID>
    ```

    **Example**:
    ```
    /copy https://t.me/c/1234567890/1000 -100987654321
    ```

    *   This will copy message `1000` and all NEWER messages from that private channel to your destination channel `-100987654321`.
    *   Make sure you (your user account) are in the source channel.
    *   Make sure your User Account is an Admin in the destination channel (so it can post).

## Notes
- The bot copies content (Download -> Upload), it does not forward.
- This creates a permanent copy.
- It waits 2 seconds between messages to avoid Telegram flood limits.
