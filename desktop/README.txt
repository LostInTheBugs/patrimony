PATRIMONY — DESKTOP APP (Windows)
==================================

Your wealth, your data, on YOUR computer.
No server, no cloud account, no installation required. The only thing
that goes online: public crypto prices (read-only explorers — never
your keys).

🌍 Other languages: Français → LISEZ-MOI.txt · Deutsch → LIESMICH.txt ·
   Lëtzebuergesch → LIES-MICH.txt

SETUP (2 minutes, no installer)
-------------------------------
1. Unzip the "Patrimony-Windows" folder anywhere you like (e.g.
   Documents\Patrimony). Avoid "Program Files": Windows blocks writes
   there.
2. Double-click "Patrimony.exe".

   First launch: Windows may show "Windows protected your PC"
   (SmartScreen — normal for a young publisher). Click "More info",
   then "Run anyway".

   Windows Defender may also flag "Behavior:Win32/DefenseEvasion" or a
   Trojan: this is a KNOWN FALSE POSITIVE for unsigned applications.
   Click the alert -> "Actions" -> "Allow on device".

3. The Patrimony window opens.

FIRST LOGIN
-----------
   Username: admin
   Password: change-me

   ⚠️ Change it right away: "Settings" menu once you are logged in.

YOUR DATA
---------
Everything lives in the "data" folder next to Patrimony.exe.
Backing up = copying that "data" folder to a USB stick or external
drive from time to time. Nothing is uploaded anywhere: followed crypto
addresses are only queried through public explorers (read-only), never
your private keys.

TO CLOSE
--------
Simply close the window (the X). The app stops.

TROUBLESHOOTING
---------------
- "Windows protected your PC": see step 2, this is normal.
- Windows Defender blocks the file: false positive (unsigned app) ->
  "Actions" -> "Allow on device". The file has been submitted to
  Microsoft for review.
- The window does not open: check that an antivirus is not blocking
  Patrimony.exe, then try again.
- Start over (erase everything): delete the "data" folder; the next
  launch starts fresh.

MIT License — open source: github.com/LostInTheBugs/patrimony
