import test from 'node:test';
import assert from 'node:assert/strict';
import {noteSections, sectionsForNote} from '../src/ChatStart';

test('separating facts and forecasts preserves the default ASO catalogue',()=>{
  for(const prompt of [
    'Подготовь аналитическую записку. Раздели фактические данные и прогнозы.',
    'Подготовьте записку. Разделите фактические данные и прогнозы.',
    'Подготовь аналитическую записку по PDF.',
  ])assert.deepEqual(sectionsForNote(prompt),noteSections);
});

test('an explicit custom structure remains in the user prompt',()=>{
  for(const prompt of [
    'Разделы: бизнес, финансы, риски.',
    'Сделай только финансовый раздел.',
    'Используй заголовки: компания и показатели.',
  ])assert.deepEqual(sectionsForNote(prompt),[]);
});
