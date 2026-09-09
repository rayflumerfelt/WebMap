/** What the map needs to know about a layer beyond its appearance. */
export interface LayerMetadata {
  datasetId: string;
  name: string;
  kind: 'vector' | 'grid' | 'pointset' | 'fault_network';
  valueRange?: { min: number; max: number; unit?: string };
  paletteId?: string;
  editable?: boolean;
}
