export const FRONTEND_BUILD = "20260617-mobile-cache-bust-1";

window.__TOPSBOTTG_FRONTEND_BUILD__ = FRONTEND_BUILD;
console.info("[topsbottg] frontend build", window.__TOPSBOTTG_FRONTEND_BUILD__);

import { bootstrap } from "./js/bootstrap.js?v=20260617-mobile-cache-bust-1";

bootstrap();
