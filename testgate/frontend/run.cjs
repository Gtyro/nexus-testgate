'use strict';

const { createRequire } = require('node:module');
const { dirname, join } = require('node:path');
const { pathToFileURL } = require('node:url');
const executionStarted = require('./phase.cjs');

const projectRequire = createRequire(join(process.cwd(), 'package.json'));
const framework = process.argv[2];
const args = process.argv.slice(3);

async function vitest() {
  const { parseCLI, startVitest } = await import(pathToFileURL(projectRequire.resolve('vitest/node')).href);
  const { filter, options } = parseCLI(['vitest', 'run', ...args]);
  // Apply the same CLI normalization as Vitest's native entry point.
  if (options.exclude) {
    options.cliExclude = Array.isArray(options.exclude) ? options.exclude : [options.exclude];
    delete options.exclude;
  }
  if (filter.some((name) => name.includes(':'))) options.includeTaskLocation ??= true;
  if (typeof options.typecheck?.only === 'boolean') options.typecheck.enabled ??= true;
  options.run = true;
  const configured = new WeakSet();
  const reporter = {
    onTestModuleCollected(module) {
      if ([...module.children.allTests()].length) executionStarted();
    },
  };
  const plugin = {
    name: 'testgate-phase-timing',
    configureVitest({ vitest: context }) {
      if (configured.has(context)) return;
      configured.add(context);
      context.config.reporters.push(reporter);
    },
  };
  const context = await startVitest('test', filter, options, { plugins: [plugin] });
  if (context.shouldKeepServer()) throw new Error('Testgate phase timing requires a single test run');
  await context.exit();
}

async function jest() {
  const jestRequire = createRequire(projectRequire.resolve('jest'));
  const cliRequire = createRequire(jestRequire.resolve('jest-cli'));
  const config = cliRequire('jest-config');
  const readConfigs = config.readConfigs;
  // Append a setup hook to each resolved project without reloading its config
  // or replacing its existing setup files, environment, runner or reporters.
  config.readConfigs = async (...parameters) => {
    const resolved = await readConfigs(...parameters);
    return {
      ...resolved,
      configs: resolved.configs.map((project) => ({
        ...project,
        setupFilesAfterEnv: [join(__dirname, 'jest-setup.cjs'), ...project.setupFilesAfterEnv],
      })),
    };
  };
  await jestRequire('jest-cli').run(args);
}

function playwright() {
  if (process.env.PW_TEST_REPORTER) {
    throw new Error('Playwright phase timing cannot replace an existing PW_TEST_REPORTER');
  }
  // Playwright appends this reporter to configured and CLI-selected reporters.
  process.env.PW_TEST_REPORTER = join(__dirname, 'playwright-reporter.cjs');
  const cli = join(dirname(projectRequire.resolve('@playwright/test/package.json')), 'cli.js');
  process.argv = [process.execPath, cli, 'test', ...args];
  require(cli);
}

Promise.resolve().then(() => ({ vitest, jest, playwright })[framework]()).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
