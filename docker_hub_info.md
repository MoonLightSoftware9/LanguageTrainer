# LanguageTrainer

A local browser-based vocabulary trainer built around typed recall, spaced reminders, meanings, books, quotes, pronunciation, and offline dictionaries.

## Features

- Multiple users and word collections
- Sprint, meanings, and reminder modes
- Book reading and quote practice
- English → Russian offline translation with Argos Translate
- StarDict and Macmillan dictionaries
- Local word pronunciation
- Persistent SQLite database
- Built-in PDF documentation available through **Read me**

## Requirements

Install Docker Desktop on Windows or macOS, or Docker Engine on Linux.

The application will be available at:

**http://localhost:8000**

Persistent application data is stored in the Docker volume `dict-data`.

---

## Windows — PowerShell

Set the image name:

```powershell
$IMAGE = "YOUR_DOCKERHUB_USERNAME/dict:0.5.0"

Pull the image and create persistent storage:

docker pull $IMAGE
docker volume create dict-data

Initialize the database and create the first user:

docker run --rm `
  -v dict-data:/data `
  $IMAGE control --init --name Alice

Install the offline English → Russian translation model:

docker run --rm `
  -v dict-data:/data `
  $IMAGE control --update --yes

Start the application:

docker run -d `
  --name dict `
  --restart unless-stopped `
  -p 8000:8000 `
  -v dict-data:/data `
  $IMAGE

Open http://localhost:8000 in your browser.

———

## macOS

Set the image name:

IMAGE="YOUR_DOCKERHUB_USERNAME/dict:0.5.0"

Pull the image and create persistent storage:

docker pull "$IMAGE"
docker volume create dict-data

Initialize the database and create the first user:

docker run --rm \
  -v dict-data:/data \
  "$IMAGE" control --init --name Alice

Install the offline English → Russian translation model:

docker run --rm \
  -v dict-data:/data \
  "$IMAGE" control --update --yes

Start the application:

docker run -d \
  --name dict \
  --restart unless-stopped \
  -p 8000:8000 \
  -v dict-data:/data \
  "$IMAGE"

Open http://localhost:8000 in your browser.

———

## Linux

Set the image name:

IMAGE="YOUR_DOCKERHUB_USERNAME/dict:0.5.0"

Pull the image and create persistent storage:

docker pull "$IMAGE"
docker volume create dict-data

Initialize the database and create the first user:

docker run --rm \
  -v dict-data:/data \
  "$IMAGE" control --init --name Alice

Install the offline English → Russian translation model:

docker run --rm \
  -v dict-data:/data \
  "$IMAGE" control --update --yes

Start the application:

docker run -d \
  --name dict \
  --restart unless-stopped \
  -p 8000:8000 \
  -v dict-data:/data \
  "$IMAGE"

Open http://localhost:8000 in your browser.

If Docker requires root privileges, add sudo before each Docker command.

———

## Container Management

View logs:

docker logs -f dict

Stop the application:

docker stop dict

Start it again:

docker start dict

Restart it:

docker restart dict

Remove the container without deleting your data:

docker rm -f dict

Your database, settings, and translation model remain stored in dict-data.

## Upgrade

Pull a newer image:

docker pull YOUR_DOCKERHUB_USERNAME/dict:0.5.0
docker rm -f dict

Then repeat the start command for your operating system. The existing dict-data volume will be reused automatically.

## Reset All Data

Warning: this permanently deletes all users, progress, settings, and translation data.

docker rm -f dict
docker volume rm dict-data

## Included Paths

- /data/dict.sqlite3 — application database
- /data/user_settings.json — user settings
- /data/argos-translate — offline translation model
- /app/docs/manual/README.md — built-in English documentation
- /app/docs/manual/README.ru.md — built-in Russian documentation

Image version: 0.5.0
