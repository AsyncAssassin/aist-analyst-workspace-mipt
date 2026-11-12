import { readFileSync, writeFileSync } from 'node:fs';
import { serializeDefinition } from '../src/contracts';
const [input, output] = process.argv.slice(2);
writeFileSync(output, serializeDefinition('VersionView', JSON.parse(readFileSync(input, 'utf8'))));
