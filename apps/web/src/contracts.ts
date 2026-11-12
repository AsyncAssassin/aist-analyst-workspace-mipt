import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import schema from '../../../contracts/v1/schemas/aist.schema.json';
import identity from '../../../contracts/v1/identity.json';

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export const contractIdentity = identity;
const ajv = new Ajv2020({ strict: false, allErrors: true, coerceTypes: false, useDefaults: false, removeAdditional: false });
addFormats(ajv);
ajv.addSchema(schema);

export function validateDefinition(name: string, value: unknown): Json {
  if (!(name in schema.$defs)) throw new Error('Unknown contract definition');
  const validate = ajv.getSchema(`${schema.$id}#/$defs/${name}`);
  if (!validate || !validate(value)) throw new Error(`Invalid ${name}: ${ajv.errorsText(validate?.errors)}`);
  return value as Json;
}

export function serializeDefinition(name: string, value: unknown): string {
  const wire = JSON.stringify(validateDefinition(name, value));
  validateDefinition(name, JSON.parse(wire));
  return wire;
}

export function object(value: Json): { [key: string]: Json } {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('Expected object');
  return value;
}

export function versionLookup(project: Json, version: Json): string {
  validateDefinition('PathProjectVersion', { project_id: project, version_id: version });
  return `/v1/projects/${encodeURIComponent(String(project))}/artifact-versions/${encodeURIComponent(String(version))}`;
}
