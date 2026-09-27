# Architecture

Loci's local demonstration runs four services. A React frontend is served by Nginx. A FastAPI application handles requests and media delivery. PostgreSQL stores objects, annotations and transcripts. Redis supports rate limiting and optional background jobs.

The browser sends API requests through Nginx to FastAPI. FastAPI reads the database and serves model and video files from a separate media volume. PostgreSQL and Redis are accessible within the Docker network. The website and API are available on the local computer.

## Where the code lives

- `apps/web/src/public` contains the object browser and public viewer entry points.
- `apps/web/src/evidence` displays objects, annotations, transcripts, video, citations and sharing controls.
- `apps/web/src/embed` provides the embedded object viewer.
- `apps/web/src/components/ModelCanvas.jsx` handles 3D rendering, annotation placement, camera movement and rendering recovery.
- `apps/web/src/App.jsx` contains the signed-in console and existing authoring tools.
- `apps/web/src/lib` contains shared media, navigation, search and citation helpers.
- `apps/api/app/api` defines API routes and request and response formats.
- `apps/api/app/models` defines database records. The API's `alembic` folder contains schema migrations.
- `apps/api/app/services` handles published content, media delivery and optional processing.
- `apps/api/app/scripts` contains tools for generating and preparing local content.

## Content review and publication

Authors sign in to create and edit records. Public endpoints return the fields approved for readers. A new annotation becomes visible after review and explicit publication. Database tests check that private authoring information stays within the authenticated tools.

Remote publishing is an optional integration. Its existing status tracking is held within a running process. Supporting reliable publication across multiple processes will require persistent operation records, background jobs and recovery procedures.

Monitoring, transcription and semantic search are optional integrations with separate configuration. The local demonstration uses lexical transcript search. Installation downloads dependencies from package registries.

## Hosting considerations

The quickstart runs on the local computer. A hosted installation needs authentication review, HTTPS, browser origin policies, secret management, database backups and an operating plan. Evaluate larger models and videos against device memory, video decoding capacity and network conditions.
