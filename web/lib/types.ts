export type Station = {
  n: string;   // name
  la: number;  // latitude
  lo: number;  // longitude
  c: number;   // dock capacity
  z: number;   // service area index
};

export type Point = {
  t: string;       // ISO timestamp of the 5-minute bucket
  bikes: number;
  ebikes: number;
  starved: number; // stations serving with zero ebikes
  empty: number;   // stations serving with zero bikes
  full: number;    // stations with no free dock
};

export type Fleet = {
  stations: Station[];
  zoneNames: string[];
  series: Point[];
  /** One char per station per bucket: 0 healthy 1 no-ebikes 2 empty 3 full 4 off 9 unknown */
  frames: string[];
  /** Ebikes available per station per bucket, capped at 35 */
  ebikes: number[][];
  /** [bucketIndex, stationIndex, bikesMoved] — sign gives direction */
  vans: [number, number, number][];
};

/** Station state codes, as they appear in `frames`. */
export const STATE = {
  HEALTHY: "0", NO_EBIKES: "1", EMPTY: "2", FULL: "3", OFF: "4", UNKNOWN: "9",
} as const;
