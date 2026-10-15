/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend origin, e.g. https://resqnet-api.onrender.com. Empty = same origin (local dev proxy). */
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
