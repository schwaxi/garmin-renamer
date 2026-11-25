"""Notfall-Login über die Konsole, falls der Login über die Weboberfläche nicht klappt.

Aufruf auf Unraid:  docker exec -it garmin-renamer python -m app.cli_login
Danach den Container neu starten.
"""
from getpass import getpass

from garminconnect import Garmin

from .garmin_client import token_dir


def main() -> None:
    email = input("Garmin E-Mail: ").strip()
    password = getpass("Garmin Passwort: ")
    g = Garmin(email=email, password=password, prompt_mfa=lambda: input("MFA-Code: ").strip())
    g.login(token_dir())
    print(f"Angemeldet. Tokens liegen in {token_dir()}. Bitte den Container neu starten.")


if __name__ == "__main__":
    main()
