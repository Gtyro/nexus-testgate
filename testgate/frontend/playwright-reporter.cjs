'use strict';

const executionStarted = require('./phase.cjs');

module.exports = class TimingReporter {
  onTestBegin() {
    executionStarted();
  }

  printsToStdio() {
    return false;
  }
};
