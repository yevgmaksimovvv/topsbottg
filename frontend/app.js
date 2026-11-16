export const FRONTEND_BUILD = "20261116-campaigns";

window.__TOPSBOTTG_FRONTEND_BUILD__ = FRONTEND_BUILD;
console.info("[topsbottg] frontend build", window.__TOPSBOTTG_FRONTEND_BUILD__);

import { bootstrap } from "./js/bootstrap.js?v=20261116-campaigns";

bootstrap();
