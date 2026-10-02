// In-process bus (vendored mitt). See CONTRACT.md section 3.
import mitt from '../vendor/mitt.mjs';

export const bus = mitt();
export { mitt };
