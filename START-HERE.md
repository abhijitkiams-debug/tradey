# ClaudeTrade: start here

ClaudeTrade **practises trading Indian stocks with pretend money** (₹10,000), using real
prices from Groww. It trades by itself during market hours and closes everything before
3:10 PM. **Real money is OFF.**

You need: a Mac or Windows computer, and a Groww account with the **Trading API plan**
(₹499/month, bought on Groww).

## One-time setup (about 15 minutes)

1. **Install Docker Desktop:** https://www.docker.com/products/docker-desktop/
   Open it once and wait until it says it's running.
2. **Get ClaudeTrade onto your computer.** Ask a grown-up to run this once in Terminal:
   `git clone https://github.com/abhijitkiams-debug/tradey` (it makes a folder called `tradey`).
3. Open the `tradey` folder and **double-click `Start ClaudeTrade`**.
   - Mac: if it says it can't be opened, right-click it → **Open** → **Open**.
   - The first start takes a few minutes. Then your browser opens ClaudeTrade by itself.
4. In the browser, follow **🔗 Connect Groww**: make a *TOTP token* on Groww, paste the two
   values into the two boxes, and click **Connect Groww**. Wait about 20 seconds.
5. Click **▶ Start practice**. Done!

## Every day

- Keep the computer **on, awake and online** on weekdays from 9:00 AM to 4:30 PM.
- Double-click **Start ClaudeTrade** whenever you want to open it.
- The page tells you in plain words what's happening. Green means good.

## The buttons

| Button | What it does |
|---|---|
| ▶ Start practice | Starts practice trading (with pretend money). |
| ⏸ Pause | No new trades. Trades already open keep their safety stops. |
| ▶ Resume | Starts again after a pause. |
| 🛑 Emergency stop | No new trades **and** closes today's open trades right now. |
| ✔ Clear emergency stop | Use it when things are OK again. |
| 🧪 Run practice | Replays the last market day to show what would have happened. Works at night too. |

## Stop or update

- **Stop ClaudeTrade:** stops it. Your history is saved.
- **Update ClaudeTrade** (Mac): gets the newest version and restarts.

## If something is wrong

Look at **Is everything OK?** on the page. Each ❌ says what to do (👉).
Never share your Groww token or secret with anyone, not even in a chat.
