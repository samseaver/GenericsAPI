import logging
import os
import shutil
import uuid

import pandas as pd
from xlrd.biffh import XLRDError

from installed_clients.DataFileUtilClient import DataFileUtil
from installed_clients.KBaseSearchEngineClient import KBaseSearchEngine
from installed_clients.WorkspaceClient import Workspace as workspaceService
from GenericsAPI.Utils.DataUtil import DataUtil


class AttributesUtil:

    def __init__(self, config):
        self.ws_url = config["workspace-url"]
        self.callback_url = config['SDK_CALLBACK_URL']
        self.token = config['KB_AUTH_TOKEN']
        self.shock_url = config['shock-url']
        self.srv_wiz_url = config['srv-wiz-url']
        self.scratch = config['scratch']
        self.dfu = DataFileUtil(self.callback_url)
        self.kbse = KBaseSearchEngine(config['search-url'])
        self.data_util = DataUtil(config)
        self.wsClient = workspaceService(self.ws_url, token=self.token)
        self.DEFAULT_ONTOLOGY_REF = "KbaseOntologies/Custom"
        self.DEFAULT_ONTOLOGY_ID = "Custom:Term"
        self.DEFAULT_UNIT_ID = "Custom:Unit"
        self.ONT_LABEL_DEL = " - "
        self.ONT_TERM_DEL = ":"

    @staticmethod
    def validate_params(params, expected, opt_param=set()):
        """Validates that required parameters are present. Warns if unexpected parameters appear"""
        expected = set(expected)
        opt_param = set(opt_param)
        pkeys = set(params)
        if expected - pkeys:
            raise ValueError("Required keys {} not in supplied parameters"
                             .format(", ".join(expected - pkeys)))
        defined_param = expected | opt_param
        for param in params:
            if param not in defined_param:
                logging.warning("Unexpected parameter {} supplied".format(param))

    def file_to_attribute_mapping(self, params):
        """Convert a user supplied file to a compound set"""
        if 'input_file_path' in params:
            scratch_file_path = params['input_file_path']
        elif 'input_shock_id' in params:
            scratch_file_path = self.dfu.shock_to_file(
                {'shock_id': params['input_shock_id'],
                 'file_path': self.scratch}
            ).get('file_path')
        else:
            raise ValueError("Must supply either a input_shock_id or input_file_path")
        try:
            df = pd.read_excel(scratch_file_path)
        except XLRDError:
            df = pd.read_csv(scratch_file_path, sep=None)
        df = df.fillna(value='').astype('str')
        if df.columns[1].lower() == "attribute ontology id":
            comp_set = self._df_to_am_obj(df)
        else:
            comp_set = self._isa_df_to_am_object(df)
        info = self.dfu.save_objects({
            "id": params['output_ws_id'],
            "objects": [{
                "type": "KBaseExperiments.AttributeMapping",
                "data": comp_set,
                "name": params['output_obj_name']
            }]
        })[0]
        return {"attribute_mapping_ref": "%s/%s/%s" % (info[6], info[0], info[4])}

    def _am_data_to_df(self, data):
        """
        Converts a compound set object data to a dataframe
        """

        attributes = pd.DataFrame(data['attributes'])
        attributes.rename(
            columns=lambda x: x.replace("ont", "ontology").capitalize().replace("_", " "))
        instances = pd.DataFrame(data['instances'])
        am_df = attributes.join(instances)

        return am_df

    def _clusterset_data_to_df(self, data):
        """
        Converts a cluster set object data to a dataframe
        """

        original_matrix_ref = data.get('original_data')
        data_matrix = self.data_util.fetch_data(
            {'obj_ref': original_matrix_ref}).get('data_matrix')

        data_df = pd.read_json(data_matrix)
        clusters = data.get('clusters')

        id_name_list = [list(cluster.get('id_to_data_position').keys()) for cluster in clusters]
        id_names = [item for sublist in id_name_list for item in sublist]

        if set(data_df.columns.tolist()) == set(id_names):  # cluster is based on columns
            data_df = data_df.T

        cluster_names = [None] * data_df.index.size

        cluster_id = 0
        for cluster in clusters:
            item_ids = list(cluster.get('id_to_data_position').keys())
            item_idx = [data_df.index.get_loc(item_id) for item_id in item_ids]

            for idx in item_idx:
                cluster_names[idx] = cluster_id

            cluster_id += 1

        data_df['cluster'] = cluster_names

        return data_df

    def _ws_obj_to_df(self, input_ref):
        """Converts workspace obj to a DataFrame"""
        res = self.dfu.get_objects({
            'object_refs': [input_ref]
        })['data'][0]
        name = res['info'][1]

        obj_type = res['info'][2]

        if "KBaseExperiments.AttributeMapping" in obj_type:
            cs_df = self._am_data_to_df(res['data'])
        elif "KBaseExperiments.ClusterSet" in obj_type:
            cs_df = self._clusterset_data_to_df(res['data'])
        else:
            err_msg = 'Ooops! [{}] is not supported.\n'.format(obj_type)
            err_msg += 'Please supply KBaseExperiments.AttributeMapping or KBaseExperiments.ClusterSet'
            raise ValueError("err_msg")

        return name, cs_df, obj_type

    def _df_to_am_obj(self, am_df):
        """Converts a dataframe from a user file to a compound set object"""
        attribute_mapping = {'ontology_mapping_method': "User Curation"}
        if not len(am_df):
            raise ValueError("No attributes in supplied files")
        attribute_df = am_df.filter(regex="[Uu]nit|[Aa]ttribute")
        instance_df = am_df.drop(attribute_df.columns, axis=1)
        if not len(instance_df.columns):
            raise ValueError("Unable to find any instance columns in supplied file")

        attribute_df.rename(columns=lambda x: x.lower().replace(" ontology ", "_ont_").strip(),
                            inplace=True)
        if "attribute" not in attribute_df.columns:
            raise ValueError("Unable to find a 'attribute' column in supplied file")
        attribute_fields = ('attribute', 'unit', 'attribute_ont_id', 'unit_ont_id')
        attributes = attribute_df.filter(items=attribute_fields).to_dict('records')

        attribute_mapping['attributes'] = [self._add_ontology_info(f) for f in attributes]
        attribute_mapping['instances'] = instance_df.to_dict('list')

        return attribute_mapping

    def _isa_df_to_am_object(self, isa_df):
        skip_columns = {'Raw Data File', 'Derived Data File', 'Array Data File', 'Image File'}
        if 'Sample Name'in isa_df.columns and not any(isa_df['Sample Name'].duplicated()):
            isa_df.set_index('Sample Name', inplace=True)
        elif 'Assay Name'in isa_df.columns and not any(isa_df['Assay Name'].duplicated()):
            isa_df.set_index('Assay Name', inplace=True)
        elif not any(isa_df[isa_df.columns[0]].duplicated()):
            logging.warning(f'Using {isa_df.columns[0]} as ID column')
            isa_df.set_index(isa_df.columns[0], inplace=True)
        else:
            raise ValueError("Unable to detect an ID column that was unigue for each row. "
                             f"Considered 'Sample Names', 'Assay Names' and {isa_df.columns[0]}")
        attribute_mapping = {'ontology_mapping_method': "User Curation - ISA format"}
        attribute_mapping['attributes'], new_skip_cols = self._get_attributes_from_isa(
            isa_df, skip_columns)
        reduced_isa = isa_df.drop(columns=new_skip_cols, errors='ignore')
        attribute_mapping['instances'] = reduced_isa.T.to_dict('list')

        return attribute_mapping

    def _get_attributes_from_isa(self, isa_df, skip_columns):
        attributes = []
        # associate attribute columns with the other columns that relate to them
        for i, col in enumerate(isa_df.columns):
            if col.startswith('Term Source REF'):
                skip_columns.add(col)
                last_attr = attributes[-1]
                if '_unit' in last_attr:
                    last_attr['_unit_ont'] = col
                else:
                    last_attr['_val_ont'] = col

            elif col.startswith('Term Accession Number'):
                # If the term Accession is a web link only grab the last bit
                # Similarly, sometimes the number is prefixed with the term source e.x. UO_0000012
                isa_df[col] = isa_df[col].map(lambda x: x.split("/")[-1].split("_")[-1])
                skip_columns.add(col)
                last_attr = attributes[-1]
                if '_unit' in last_attr:
                    last_attr['_unit_accession'] = col
                else:
                    last_attr['_val_accession'] = col

            elif col.startswith('Unit'):
                skip_columns.add(col)
                last_attr = attributes[-1]
                if last_attr.get('unit'):
                    raise ValueError("More than one unit column is supplied for attribute {}"
                                     .format(last_attr['attribute']))
                last_attr['_unit'] = col

            elif col not in skip_columns:
                attributes.append({"attribute": col})

        # handle the categories for each attribute
        for i, attribute in enumerate(attributes):
            if '_val_accession' in attribute:
                category_df = isa_df[[attribute['attribute'], attribute.pop('_val_ont'),
                                     attribute.pop('_val_accession')]].drop_duplicates()
                category_df['attribute_ont_id'] = category_df.iloc[:, 1].str.cat(
                    category_df.iloc[:, 2], ":")
                category_df['value'] = category_df[attribute['attribute']]
                attribute['categories'] = category_df.set_index(
                    attribute['attribute'])[['value', 'attribute_ont_id']].to_dict('index')

            if '_unit' in attribute:
                units = isa_df[attribute.pop('_unit')].unique()
                if len(units) > 1:
                    raise ValueError("More than one unit type is supplied for attribute {}: {}"
                                     .format(attribute['attribute'], units))
                attribute['unit'] = units[0]

                if '_unit_ont' in attribute:
                    unit_ont = isa_df[attribute.pop('_unit_ont')].str.cat(
                        isa_df[attribute.pop('_unit_accession')], ":").unique()
                    if len(units) > 1:
                        raise ValueError("More than one unit ontology is supplied for attribute "
                                         "{}: {}".format(attribute['attribute'], unit_ont))
                    attribute['unit_ont_id'] = unit_ont[0]
            attributes[i] = self._add_ontology_info(attribute)
        return attributes, skip_columns

    def _search_ontologies(self, term, closest=False):
        """
        Match to an existing KBase ontology term
        :param term: Test to match
        :param closest: if false, term must exactly match an ontology ID
        :return: dict(ontology_ref, id)
        """
        params = {
            "object_types": ["OntologyTerm"],
            "match_filter": {
                "lookup_in_keys": {"id": {"value": term}}
            },
            "access_filter": {
                "with_private": 0,
                "with_public": 1
            },
            "pagination": {
                "count": 1
            },
            "post_processing": {
                "skip_data": 1
            }
        }
        if closest:
            params['match_filter'] = {"full_text_in_all": term}
        res = self.kbse.search_objects(params)
        if not res['objects']:
            return None
        term = res['objects'][0]
        return {"ontology_ref": term['guid'].split(":")[1], "id": term['key_props']['id']}

    def _add_ontology_info(self, attribute):
        """Searches KBASE ontologies for terms matching the user supplied attributes and units.
        Add the references if found"""
        optionals = {"unit", "unit_ont_id", "unit_ont_ref", }
        attribute = {k: v for k, v in attribute.items() if k not in optionals or v != ""}
        ont_info = self._search_ontologies(attribute.get('attribute_ont_id', "").replace("_", ":"))
        if ont_info:
            attribute['attribute_ont_ref'] = ont_info['ontology_ref']
            attribute['attribute_ont_id'] = ont_info['id']
        else:
            attribute['attribute_ont_ref'] = self.DEFAULT_ONTOLOGY_REF
            attribute['attribute_ont_id'] = self.DEFAULT_ONTOLOGY_ID

        if attribute.get('unit'):
            ont_info = self._search_ontologies(attribute.get('unit_ont_id', '').replace("_", ":"))
            if ont_info:
                attribute['unit_ont_ref'] = ont_info['ontology_ref']
                attribute['unit_ont_id'] = ont_info['id']
            else:
                attribute['unit_ont_ref'] = self.DEFAULT_ONTOLOGY_REF
                attribute['unit_ont_id'] = self.DEFAULT_UNIT_ID
        return attribute

    def to_tsv(self, params):
        """Convert an compound set to TSV file"""
        files = {}

        _id, df, obj_type = self._ws_obj_to_df(params['input_ref'])
        files['file_path'] = os.path.join(params['destination_dir'], _id + ".tsv")
        df.to_csv(files['file_path'], sep="\t", index=False)

        return _id, files

    def to_excel(self, params):
        """Convert an compound set to Excel file"""
        files = {}

        _id, df, obj_type = self._ws_obj_to_df(params['input_ref'])
        files['file_path'] = os.path.join(params['destination_dir'], _id + ".xlsx")

        writer = pd.ExcelWriter(files['file_path'])

        if "KBaseExperiments.AttributeMapping" in obj_type:
            df.to_excel(writer, "Attributes", index=False)
        elif "KBaseExperiments.ClusterSet" in obj_type:
            df.to_excel(writer, "ClusterSet", index=True)
        # else is checked in `_ws_obj_to_df`

        writer.save()

        return _id, files

    def export(self, file, name, input_ref):
        """Saves a set of files to SHOCK for export"""
        export_package_dir = os.path.join(self.scratch, name + str(uuid.uuid4()))
        os.makedirs(export_package_dir)
        shutil.move(file, os.path.join(export_package_dir, os.path.basename(file)))

        # package it up and be done
        package_details = self.dfu.package_for_download({
            'file_path': export_package_dir,
            'ws_refs': [input_ref]
        })

        return {'shock_id': package_details['shock_id']}
