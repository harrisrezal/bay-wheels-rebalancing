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
  full: number;   // stations with no free dock
  short?: number;  // stocked but short of expected demand
  doomed?: number; // stocked but under an hour of runway
  watch?: number;  // under three hours of runway
};

export type Fleet = {
  stations: Station[];
  zoneNames: string[];
  series: Point[];
  /** One char per station per bucket: 0 healthy 1 no-ebikes 2 empty 3 full 4 off 9 unknown */
  frames: string[];
  /** Same shape, but relative to demand: 0 adequate 1 starved-under-demand
   *  5 stocked-but-short-of-demand 4 off 9 unknown */
  dframes: string[];
  /** Runway state: 0 fine  1 under 3h of stock  2 under 1h (unsavable)  3 empty  4 off */
  rframes: string[];
  /** Ebikes available per station per bucket, capped at 35 */
  ebikes: number[][];
  /** [bucketIndex, stationIndex, bikesMoved] — sign gives direction */
  vans: [number, number, number][];
};

/** Station state codes, as they appear in `frames`. */
export const STATE = {
  HEALTHY: "0", NO_EBIKES: "1", EMPTY: "2", FULL: "3", OFF: "4",
  /** Stocked, but holding less than an hour of expected demand. */
  SHORT: "5",
  /** Runway states, used by the At-risk view. */
  DOOMED: "2", WATCH: "1",
  UNKNOWN: "9",
} as const;
