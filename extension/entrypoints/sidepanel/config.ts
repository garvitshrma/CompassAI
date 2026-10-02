// Where the Compass backend lives.
//
// The default is the FastAPI server on Render. To use a server on your own
// computer while developing, create extension/.env.local with:
//   WXT_API_URL=http://localhost:8000
// and restart `npm run dev`.
//
// Never put API keys here: everything in the extension can be read by anyone
// who installs it. Keys (like GROQ_API_KEY) stay on the server.

export const API_URL: string = (import.meta.env.WXT_API_URL as string | undefined) || 'https://compassai-vkoe.onrender.com';

/** How long to wait for an answer before falling back to keyword matching. */
export const ANSWER_TIMEOUT_MS = 20_000;

/**
 * Render's free plan puts the server to sleep after ~15 minutes without traffic,
 * and waking it takes up to a minute. We ping it as soon as the panel opens, so
 * it's usually awake by the time the person has typed a question.
 */
export function wakeServer() {
  fetch(`${API_URL}/health`).catch(() => {
    // Ignored: this is only a wake-up call.
  });
}
