'use strict';

const { writeFileSync } = require('node:fs');
const { join } = require('node:path');

module.exports = function executionStarted() {
  try {
    writeFileSync(join(__dirname, 'phase'), 'execution\n', { flag: 'wx' });
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
  }
};
