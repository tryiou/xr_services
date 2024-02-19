import yaml


def check_and_modify_docker_compose_file():
    # Path to the docker-compose.yml file
    docker_compose_path = "../docker-compose.yml"

    # Load the YAML file
    print("Loading docker-compose.yml file...")
    with open(docker_compose_path, 'r') as file:
        docker_compose_data = yaml.safe_load(file)

    # Define the sequence to check for
    sequence_to_check = {
        "xr_service_cg_proxy": {
            "build": "./xr_services",
            "restart": 'no',
            "stop_signal": "SIGINT",
            "stop_grace_period": "5m",
            "logging": {
                "driver": "json-file",
                "options": {
                    "max-size": "2m",
                    "max-file": "10"
                }
            },
            "networks": {
                "backend": {
                    "ipv4_address": "172.31.11.3"
                }
            }
        }
    }

    # Check if the sequence exists in the docker-compose.yml file
    print("Checking if sequence exists in docker-compose.yml file...")
    sequence_found = False
    for service_name, service_data in docker_compose_data.get('services', {}).items():
        if service_name == 'xr_service_cg_proxy' and service_data == sequence_to_check['xr_service_cg_proxy']:
            sequence_found = True
            break

    # If the sequence is not found, add it after the line containing "#### END UTXO STACK ####"
    if not sequence_found:
        print("Sequence not found. Adding sequence after '#### END UTXO STACK ####'...")
        with open(docker_compose_path, 'r') as file:
            lines = file.readlines()

        # Find the index of the line containing "#### END UTXO STACK ####"
        end_line_index = next((i for i, line in enumerate(lines) if "#### END UTXO STACK ####" in line), -1)
        if end_line_index != -1:
            # Indent the sequence properly
            indented_sequence = yaml.dump(sequence_to_check).strip()
            indented_sequence = "\n".join(["  " + line for line in indented_sequence.splitlines()])

            # Insert the indented sequence after the end line
            lines.insert(end_line_index + 1, indented_sequence + "\n")
            with open(docker_compose_path, 'w') as file:
                file.writelines(lines)
            print("Sequence added successfully.")
        else:
            print("Line '#### END UTXO STACK ####' not found. Sequence not added.")
    else:
        print("Sequence already exists in docker-compose.yml file. No action required.")


def modify_start_xrproxy_script():
    start_xrproxy_path = "../scripts/start-xrproxy.sh"

    # Define the sequence to search for and add if missing
    sequence_to_add = (
        "set-ph = RPC_cg_coins_data_HOSTIP=xr_service_cg_proxy\n"
        "set-ph = RPC_cg_coins_data_PORT=8080\n"
        "set-ph = RPC_cg_coins_data_USER=A\n"
        "set-ph = RPC_cg_coins_data_PASS=B\n"
        "set-ph = RPC_cg_coins_data_METHOD=cg_coins_data\n"
        "\n"
        "set-ph = RPC_cg_coins_list_HOSTIP=xr_service_cg_proxy\n"
        "set-ph = RPC_cg_coins_list_PORT=8080\n"
        "set-ph = RPC_cg_coins_list_USER=A\n"
        "set-ph = RPC_cg_coins_list_PASS=B\n"
        "set-ph = RPC_cg_coins_list_METHOD=cg_coins_list\n"
    )

    # Read the contents of the file
    print("Reading contents of file:", start_xrproxy_path)
    with open(start_xrproxy_path, 'r') as file:
        content = file.read()

    # Find the index of the second "EOL"
    second_eol_index = content.find("EOL", content.find("EOL") + 1)

    if second_eol_index != -1:
        # Insert a blank line before the second "EOL"
        print("Inserting a blank line before the second 'EOL' in file: ", start_xrproxy_path)
        content = content[:second_eol_index] + "\n\n" + content[second_eol_index:]

        # Check if the sequence already exists
        if sequence_to_add in content:
            print("Sequence already exists in the file: ", start_xrproxy_path)
            return

        # Insert the sequence before the second "EOL"
        print("Inserting sequence into file: ", start_xrproxy_path)
        updated_content = content[:second_eol_index] + "\n" + sequence_to_add + content[second_eol_index:]

        # Write the modified content back to the file
        with open(start_xrproxy_path, 'w') as file:
            file.write(updated_content)

        print("Sequence added successfully to file: ", start_xrproxy_path)
    else:
        print("Second 'EOL' not found in file: ", start_xrproxy_path, ". Sequence not added.")


# Call the function to modify the start-xrproxy.sh file
print("modify the start-xrproxy.sh file")
modify_start_xrproxy_script()
print()
# Call the function to check and modify the docker-compose.yml file
print("modify the docker-compose.yml file")
check_and_modify_docker_compose_file()
