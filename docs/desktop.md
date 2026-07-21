# Desktop client

The Tauri v2 shell loads the same `apps/web` React build used by the portal. Run `cd desktop && npm install && npm run dev`; release builds use `npm run build`. The server remains authoritative, so a file submitted from Windows or macOS receives the same hash, parser, rules, model version, quality gates, and result.

`desktop/src/adapter.ts` provides the browser/desktop boundary. A user must explicitly select files or a watched directory before Rust will read it. Reads are limited to 8 MiB chunks, enabling the web client to use the server's resumable upload API without exposing unrestricted filesystem access. Directory events only include supported business-file extensions. Server hash deduplication remains authoritative.

On Windows, `openPowerBiFile` asks the operating system to open a PBIX with its registered application. The user must have a licensed Power BI Desktop installation. On macOS/Linux, the command returns an explicit unsupported-platform error. `startPbixToPbipAssistance` only opens Power BI Desktop and directs an operator to Microsoft's supported Save As flow; it is not an automated PBIX-to-PBIP converter.

Production desktop builds should replace the permissive customer-server `connect-src` wildcard with the customer's exact HTTPS origin, be code-signed/notarized for each platform, and be distributed through the customer's managed software channel.
