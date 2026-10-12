// Names for the measured circuits, shared by the dashboards.
// Keys are iotawatt_readings columns, which are the IoTaWatt series names in lower case
// (Mains_A → mains_a), plus the derived upstairs values from house_minutes.
// Same names as the morning report (src/dorfsteig_control/report.py).

// Two-phase (400 V) loads with a CT on one leg only: they draw twice the channel
// (see TWO_PHASE_CHANNELS in src/dorfsteig_core/iotawatt.py)
const TWO_PHASE = new Set(['water_heater', 'oven', 'stovetop']);

const CIRCUIT_GROUPS = [
  { title: 'Mains (2 of 3 phases)', circuits: {
      mains_a: 'Mains A',
      mains_b: 'Mains B' } },
  { title: 'Zones', circuits: {
      zone_a: 'Zone A (kitchen, bath, dining)',
      zone_b: 'Zone B (hall, living, entrance)',
      zone_c: 'Zone C (bath up, parents, playroom S) · 3rd phase',
      zone_d: 'Zone D (upstairs hallway)',
      zone_e: 'Zone E (kids, playroom N, balcony)',
      zone_f: 'Zone F' } },
  { title: 'Appliances', circuits: {
      water_heater: 'Water heater downstairs · 2 phases',
      oven: 'Oven · 2 phases',
      stovetop: 'Stovetop · 2 phases',
      dishwasher: 'Dishwasher',
      clothes_washer: 'Washer · 3rd phase',
      clothes_dryer: 'Dryer' } },
  { title: 'Upstairs (house − downstairs)', circuits: {
      upstairs_water: 'Water heater upstairs (estimated)',
      upstairs_other: 'Upstairs other' } },
  { title: 'Supply', circuits: {
      input_0: 'Voltage' } },
];

// column → { label, group }
const CIRCUITS = {};
for (const group of CIRCUIT_GROUPS) {
  for (const [column, label] of Object.entries(group.circuits)) {
    CIRCUITS[column] = { label, group: group.title };
  }
}

// A readable name for any column or IoTaWatt series, including ones not listed above
function circuitLabel(name) {
  const known = CIRCUITS[name.toLowerCase()];
  return known ? known.label : name.replace(/_/g, ' ');
}
