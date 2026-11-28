'use strict';

const { beforeAll: registerBeforeAll, jest: jestGlobals } = require('@jest/globals');
const { writeFileSync } = jestGlobals.requireActual('node:fs');
const { join } = jestGlobals.requireActual('node:path');

registerBeforeAll(() => {
  try {
    writeFileSync(join(__dirname, 'phase'), 'execution\n', { flag: 'wx' });
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
  }
});
